# 深空测控时间归一化服务

把一批 GPS 周秒事件与带闰秒的 UTC 事件归一到**同一条 TAI 纳秒时间轴**，
用于避免在闰秒边界错排遥测与遥控指令。

- 纯 Python 3.11 标准库实现，**所有换算只用整数运算**，无任何浮点参与；
- 闰秒表采用公开的 IANA tzdb `leap-seconds.list`，版本为加入
  2016-12-31 正闰秒的那一版（tzdata 2016g / IERS Bulletin C 52），
  共 28 行，末行偏移 37 s；
- 该表文件自带的过期声明（`#@`）为 **2017-06-28T00:00:00Z**，
  服务支持的年代为 `[1972-01-01T00:00:00Z, 2017-06-28T00:00:00Z)`；
- 输入支持 UTC 秒值 `60`（仅允许出现在闰秒日的 `23:59:60`）。

## 运行

```bash
# 宿主机端口可配置（默认 8080），一次性 verify 服务退出后整个编排退出：
HOST_PORT=9090 docker compose up \
  --abort-on-container-exit --exit-code-from verify
```

- `api`：常驻 HTTP 服务，带容器 `HEALTHCHECK`（轮询 `/healthz`）。
- `verify`：一次性容器，等待 API 健康后依次执行
  1. 单元测试（`python -m unittest discover`）；
  2. 镜像构建检查（全部源码字节编译、入口点导入、Dockerfile/compose
     静态校验；若挂载了 Docker CLI 与 socket 还会执行
     `docker build --check`）；
  3. 真实 HTTP 冒烟测试：跨 2016-12-31 闰秒，以及同一 10 位周号在
     两个 1024 周纪元（1997 与 2016/2017）的解析与歧义拒绝；

  最后以退出码报告结论并自行退出（0 全部通过，1 有失败阶段）。

仅启动服务：

```bash
docker compose up -d api
curl -s localhost:8080/healthz
```

## 接口

`POST /api/times/normalize`，每批 **1–200** 个事件，事件 `id` 必须唯一
（字符串或整数；`7` 与 `"7"` 视为同一个编号）。结果严格保持输入顺序。

### UTC 事件

```json
{
  "events": [
    {"id": "tm-1", "utc": "2016-12-31T23:59:60.5Z"}
  ]
}
```

- 必须以 `Z` 结尾；小数部分最多 9 位（纳秒）；
- 秒值 `60` 仅在闰秒日（如 2016-12-31）的 `23:59:60` 合法。

### GPS 事件

```json
{
  "events": [
    {"id": "tm-2", "gpsWeek": 1930, "gpsSecondsInWeek": 17,
     "gpsNanoseconds": 500000000}
  ]
}
```

- `gpsSecondsInWeek ∈ [0, 604799]`，`gpsNanoseconds ∈ [0, 999999999]`，
  周序号为非负整数（周 0 始于 1980-01-06T00:00:00Z）；
- GPS 恒定领先 TAI 19 s，闰秒只影响换算出的 UTC 标签。
- `gpsNanoseconds` 省略时按 0 处理。

### 模周 GPS 事件（10 位周号）

早期接收机只保存 10 位 GPS 周号（每 1024 周 ≈ 19.6 年重复一次）。
提交 `gpsWeekModulo` 与采集时刻提示 `referenceUtc`，服务在闰秒表
支持年代内解析出唯一的完整 GPS 周：

```json
{
  "events": [
    {"id": "tm-3", "gpsWeekModulo": 906, "gpsSecondsInWeek": 17,
     "gpsNanoseconds": 500000000, "referenceUtc": "2017-01-01T00:00:00Z"}
  ]
}
```

- `gpsWeekModulo ∈ [0, 1023]`；`gpsSecondsInWeek`、`gpsNanoseconds`
  与完整 GPS 事件同义，`referenceUtc` 采用与 `utc` 相同的格式
  （同样要求在支持年代内）；
- 解析规则：在支持年代内枚举同余（mod 1024）的完整周，唯一胜者须
  距参考时刻**严格小于 512 周**；距离按精确 TAI 纳秒整数比较，
  不经过浮点；
- 成功结果保留原有响应字段，并额外回显 `"resolvedGpsWeek"`
  （十进制字符串，仅模周事件携带；完整 GPS 周与 UTC 事件的请求、
  响应与顺序完全不变）；
- 下列情形整批失败：年代内无同余周距参考时刻严格小于 512 周
  （无候选）、候选周恰距参考时刻 512 周（1024 周纪元中点，歧义）、
  模周字段与 `utc`/`gpsWeek` 混用或缺 `referenceUtc`、参考时刻或
  解析结果超出支持年代。

### 响应

```json
{
  "results": [
    {
      "id": "tm-1",
      "taiNanoseconds": "1483228836500000000",
      "utc": "2016-12-31T23:59:60.5Z",
      "utcTaiOffsetSeconds": "-36"
    }
  ]
}
```

- `taiNanoseconds`：十进制**字符串**形式的 TAI 纳秒
  （自 1970-01-01T00:00:00 TAI 起的整数纳秒，可为任何大整数）；
- `utc`：规范 UTC 表示（闰秒渲染为 `23:59:60`，小数尾随零被裁掉）；
- `utcTaiOffsetSeconds`：该时刻 UTC−TAI 偏移（闰秒进行中仍为旧值，
  例如 2016-12-31T23:59:60 为 `-36`，2017-01-01T00:00:00 起为 `-37`）；
- `resolvedGpsWeek`：**仅模周事件**回显，解析出的完整 GPS 周号
  （十进制字符串）。

**同一物理时刻**无论用 UTC 还是 GPS 提交，三项结果完全一致，
包括逐字节相同的十进制 TAI 字符串。

### 错误（整批失败，绝不返回部分结果）

非法日期、越界周内秒、非真实闰秒位置、超出支持年代、模周无候选或
恰处 512 周歧义中点、输入种类字段混用、重复编号、批大小越界、
浮点 JSON 数字等都会让**整批请求**失败：

```json
{
  "error": {
    "code": "INVALID_EVENT",
    "message": "seconds value 60 is not a real leap second position: 2016-12-30T23:59:60 is not in the leap-second table",
    "eventIndex": 1,
    "eventId": "ev-42"
  }
}
```

错误体不含任何归一化结果；`eventIndex` 为从 0 开始的批次位置，
`eventId` 回显调用方编号，`message` 给出可定位的原因。

## 换算要点

- TAI 是均匀秒计数；UTC 标签在边界处重复。闰秒物理区间
  `[边界−1 s, 边界)` 映射到闰秒日的 `23:59:60[.frac]`，
  该区间内 TAI−UTC 仍为旧值。
- 公历换算使用 Hinnant 的 `days_from_civil / civil_from_days`
  整数算法，配合整数 `divmod` 拆分时分秒与纳秒。
- HTTP 层用 `json.loads(parse_int=…, parse_float=拒绝)` 解析：
  JSON 整数以标记字符串承载再转 `int`，从源头杜绝数字经过 `float`。

## 目录

```
app/timecore.py        # 闰秒表 + GPS/UTC/TAI 整数换算 + 模周解析
app/server.py          # POST /api/times/normalize、/healthz
tests/                 # unittest 用例（56 个）
verify/entrypoint.py   # 一次性 verify 编排
verify/smoke_http.py   # 跨闰秒 + 跨 1024 周纪元 HTTP 冒烟
Dockerfile, docker-compose.yml
```
