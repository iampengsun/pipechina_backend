# 气象与空间工具验证

QWeather预警、高德周边/路线和ArcGIS企业图层查询已接入原调查工具、动态分支、REST和MCP。新增人工确认的站场WGS84位置版本，保留来源、设备关联及有效期；没有新增容器或前端。

- 全量测试218通过、2项按配置跳过，分支覆盖率78%；启用真实ES、Qdrant及Neo4j隔离测试。
- 最终18项相关用例通过，涵盖坐标转换、预警过期/取消、位置版本、组织隔离、失败与缺配置、日志脱敏、智能体证据和技能版本。
- Ruff、格式、mypy、编译及差异检查通过；224个HTTP操作全部有文档条目，本地文档链接有效。
- 本机数据库迁移至0003_station_locations，共85张应用表；API、Worker及Beat使用更新后的项目镜像，就绪接口200；MCP发现18个工具，新增4个工具均只读。
- 真实HTTP查询在缺少位置时返回needs_location，未生成模拟预警、坐标或路线。

外部HTTP测试使用按官方协议构造的合成响应。当前没有配置第三方凭证、企业图层或真实站场位置，尚未证明真实服务可用及数据覆盖。需要填写QWeather API Host和Key或JWT、高德Web服务Key；企业GIS另需按组织配置图层地址及认证信息。然后登记有来源的真实WGS84坐标再做实连。

[配置与接口](../../docs/第14章_气象预警与GIS地图工具.md)、[全量结果](20261110-site-context-tests.xml)、[最终相关用例](20261110-site-context-followup-tests.xml)、[本机HTTP结果](20261110-site-context-live.json)、[源码摘要及统计](20261110-site-context-verification.json)。
