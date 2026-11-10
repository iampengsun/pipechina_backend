# 第14章：气象预警、企业GIS与地图工具

本章会先介绍GIS与坐标系，再使用气象和地图接口。它们经Provider端口接入：业务服务负责人工位置、权限和版本，HTTP适配器负责实际协议。外部数据的查询时间与有效期必须保留，不能把旧预警变成永久事实。

相关基础：[外部HTTP与适配器](基础知识/01_Web接口与配置.md#t15)、[访问范围](基础知识/03_任务权限与文件存储.md#t13)、[时间及来源快照](基础知识/07_部署监控与工程验证.md#t41)。

<a id="t42"></a>
<a id="t44"></a>

## 14.1 先认识GIS、地图和气象接口

GIS是地理信息系统：把位置、形状和业务属性联系起来。例如，一条管段可以有线路几何、设备编号和维护单位。普通地图适合查地址、周边道路和到场路线；企业GIS可以提供企业自己的管段、站场或其他授权图层。地图上的临近关系不能代替人工确认的设备归属。

气象API按位置返回外部发布的预警。预警有发布机构、发布时间、生效时间和到期时间；“未返回数据”可能表示无可用数据或覆盖不足，不能直接推出没有气象风险。预警也不能代替现场实测或作业审批。

气象与空间查询使用三类HTTP服务，沿用后端与Worker运行进程：

| 服务 | 已实现用途 | 接入方式 |
|---|---|---|
| 和风天气 QWeather | 查询当前气象预警，保留发布时间与有效期 | 官方天气预警API |
| 高德 Web服务 | 站场逆地理编码、周边设施/道路、普通驾车到场路线 | 坐标转换、逆地理编码、路径规划API |
| ArcGIS Enterprise / Online | 查询站场周边的企业图层要素 | FeatureServer或MapServer图层的只读query接口 |

企业GIS适配器目前支持ArcGIS REST图层。其他GIS产品可按[端口](../app/ports/site_context.py)实现适配器，需要实际接口文档后再对接。不会将任意网址、SQL或图层地址交给模型执行。

<a id="t43"></a>

## 14.2 经纬度与坐标系

经度表示东西方向，范围-180到180；纬度表示南北方向，范围-90到90。WGS84用于本项目保存的人工确认站场位置。高德使用GCJ02坐标，本项目先调用官方坐标转换接口，再使用转换后的坐标查询地图或规划路线。ArcGIS请求明确指定输入、输出坐标系为EPSG:4326，即本项目使用的WGS84经纬度。

需要特别区分参数顺序：

- 本项目接口用两个明确字段：`latitude`为纬度、`longitude`为经度。
- 和风天气路径是`纬度/经度`。
- 高德及本项目ArcGIS查询参数用`经度,纬度`。

不会把GCJ02数值直接当作WGS84保存，也不会从事件描述中让模型猜坐标。没有有效位置时，工具返回待补充项。

## 14.3 位置如何接到现有业务

查询依据沿现有链路确定：

```text
已确认事件 → 当前有效的人工设备关联 → 设备所属站场
→ 站场最新人工确认位置 → 外部只读查询 → 带来源与时效的调查证据
```

设备关联须对应当前事件版本，设备须有效，事件和站场须在调用者权限范围内。关联多个站场时要求指定目标站场；不会任意选一个地点。路线的出发站场也须位置有效、可见，并与事件属于同一组织。

`station_locations`保存追加式位置版本。修改不会覆盖旧位置；撤回也生成新版本。通过站场锁及`expected_version`检查并发更新，记录来源、操作人和理由。

首次登记：

```http
PUT /api/v1/stations/{station_id}/location
```

```json
{
  "expected_version": 0,
  "coordinate_system": "WGS84",
  "latitude": 39.9,
  "longitude": 116.4,
  "active": true,
  "address": "以实际测绘资料为准",
  "source": "站场测绘成果及版本编号",
  "reason": "人工核对站场位置"
}
```

上面是软件示例坐标，不代表真实站场。后续更新提交当前版本；撤回时提交`active:false`，不传经纬度，并填写来源与理由。读取当前记录使用`GET /api/v1/stations/{station_id}/location`，未登记时为`null`。

## 14.4 配置外部服务

`.env.example`和本机`.env`已留出配置项。没有密钥时服务仍可启动；工具明确返回`not_configured`，不会生成模拟预警或路线。

```dotenv
# 和风控制台分配的API Host，需包含https://；两种认证方式只填一种。
QWEATHER_API_HOST=https://你的专属域名.qweatherapi.com
QWEATHER_API_KEY=
QWEATHER_JWT=

# 申请Web服务API类型的Key。
AMAP_API_KEY=

# 本项目组织UUID -> 该组织获授权的只读ArcGIS图层。
ENTERPRISE_GIS_LAYERS={"组织UUID":"https://gis.example.com/arcgis/rest/services/approved/FeatureServer/0"}
ENTERPRISE_GIS_TOKEN=
ENTERPRISE_GIS_FIELDS=OBJECTID,name,type

SITE_CONTEXT_RADIUS_METERS=3000
SITE_CONTEXT_TIMEOUT_SECONDS=10
SITE_CONTEXT_TTL_SECONDS=300
```

示例中的组织UUID和域名必须替换为实际值；不用企业GIS时保留`ENTERPRISE_GIS_LAYERS={}`。每个组织只查询自己配置的图层，没有映射时不会改查其他组织的图层。图层本身应是获授权的数据范围。返回字段受`ENTERPRISE_GIS_FIELDS`限制。

QWeather支持API Key或JWT。ArcGIS使用Bearer Token请求头，适用于支持请求头认证的ArcGIS服务；若企业采用其他登录或网关认证，需要提供对应说明。过期凭证会返回外部服务不可用，不会被解释成空结果。配置修改后重建服务进程使其生效，沿用项目专用Compose启动方式。

## 14.5 智能体、REST和MCP共用工具

| 工具 | 作用 | 内部调查的query |
|---|---|---|
| `get_weather_alerts` | 当前预警 | 留空自动确定单一关联站场，或指定目标站场UUID |
| `get_map_context` | 地址、周边设施和道路参考 | 同上 |
| `query_enterprise_gis` | 当前组织图层的空间邻域查询 | 同上 |
| `get_access_route` | 普通驾车到场路线 | `出发站场UUID`或`出发站场UUID,目标站场UUID` |

协调者可以按需要选择`weather`、`geography`、`enterprise_gis`子调查，Evidence角色也可以直接调用。室外作业、气象异常或到场条件影响处置时才取这些证据，不要求每次调查都增加环境分支。

REST入口：

```text
GET /api/v1/events/{event_id}/site-context/{operation}
```

`operation`为上表工具名，查询参数可传`station_id`；路线还需`origin_station_id`。MCP调用同名工具，参数使用明确的UUID字段。两种入口和内部调查共用同一个应用服务，要求事件读取与设备读取权限；位置登记要求设备管理权限。

内部工具结果保存为`AgentEvidence`，来源标签为`site-context`。保留事件版本、站场位置版本、设备关联版本、查询时间、有效截止时间、服务来源和响应摘要；不把密钥、令牌或原始上游错误写入证据。

## 14.6 时效、失败与审批边界

`needs_location`表示缺少有效位置或设备关联；`needs_selection`表示需要选择目标站场；`needs_origin`表示路线缺少出发站场。`not_configured`表示配置不足，`unavailable`表示本次上游请求失败，`no_data`表示未取得可用数据，均不等于现场安全。

QWeather过滤已过期、取消和明确被替代的消息，保留发布机构与时间、级别、原始说明及归属声明。最多返回20条；忽略部分消息或截断内容时标明范围受限。预警本身的到期时间早于本地有效期时，采用更早的到期时间。

地图邻域最多返回10个设施和10条道路。逆地理编码半径最多3000米，企业GIS半径受配置限制。ArcGIS最多返回20项，并保留`limited`及大几何被省略的标记。路线返回一个普通驾车方案和有限步骤，距离与时间来自服务返回；它不是安全疏散规划，不计算泄漏扩散或隔离区域。

外部HTTP请求有超时、响应体积和结果数量上限；内部工具调用计入整次调查预算。第三方API可能单独计费，这些请求次数不计作LLM Token费用。凭证在请求日志中脱敏，不写进运行快照。

请求结束后再次核对事件、位置和设备关联版本。模型回查证据、调查完成及人工批准建议时检查时效；过期需重新取证。历史读取保留原始有效期；同一操作和站场的刷新结果替代旧快照参与当前分析，离线重放按原调查完成时刻取证；给模型的历史摘要会排除已过期上下文。

## 14.7 代码与验证

先读[站场位置服务](../app/modules/operational_graph/application/locations.py)，理解人工确认；再读[调查应用服务](../app/modules/operation_event/application/site_context.py)，看业务关联与权限；最后读[HTTP适配器](../app/infrastructure/site_context/http.py)，看三类服务的真实请求和返回处理。

[test_site_context.py](../tests/test_site_context.py)使用官方格式的合成HTTP响应，检查坐标转换、过期/取消预警、组织隔离、缺配置、失败、MCP调用及智能体证据。实际账号的服务联调仍需填入凭证和真实授权位置，模拟HTTP测试不能证明真实数据覆盖。

官方协议参考：[QWeather预警](https://dev.qweather.com/en/docs/api/warning/weather-alert/)、[QWeather认证](https://dev.qweather.com/en/docs/configuration/authentication/)、[高德坐标转换](https://developer.amap.com/api/webservice/guide/api/convert)、[高德逆地理编码](https://developer.amap.com/api/webservice/guide/api/georegeo)、[高德路线](https://developer.amap.com/api/webservice/guide/api/direction)、[ArcGIS图层查询](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/)。

## HTTP接口速查

| 方法 | 路径 | 请求体Schema | 处理函数 |
|---|---|---|---|
| GET | `/api/v1/stations/{station_id}/location` | — | [station_location](../app/modules/operational_graph/api/router.py) |
| PUT | `/api/v1/stations/{station_id}/location` | `StationLocationInput` | [confirm_station_location](../app/modules/operational_graph/api/router.py) |
| GET | `/api/v1/events/{event_id}/site-context/{operation}` | — | [event_site_context](../app/modules/operation_event/api/router.py) |
