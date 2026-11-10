# Web接口、输入校验与外部调用基础

本篇对应技术清单02、03、15。先用小例子理解请求怎样进入函数，再回到第1、2章读项目装配。示例中的独立应用只用于学习，不是项目新增接口。

<a id="t02"></a>
## 02 FastAPI、Uvicorn、ASGI、REST、OpenAPI与CORS

**先理解分工。** HTTP是一种请求/响应协议；客户端发出方法、路径、请求头和可选正文，服务器返回状态码及正文。REST是一种围绕资源组织接口的设计方式，例如用GET读取记录、POST提交业务动作。它不要求所有业务都强行套用增删改查。

FastAPI负责将请求映射到处理函数；Uvicorn负责监听端口、建立连接并运行应用。二者通过ASGI约定交换请求、响应及连接生命周期。ASGI是接口规范，不是另一个需要单独启动的数据库或消息服务。

**最小用法。** 将下面内容放入独立学习目录的`demo.py`，用`uvicorn demo:app --port 8099`启动，然后访问`http://127.0.0.1:8099/items/3`：

```python
from fastapi import FastAPI

app = FastAPI()


@app.get("/items/{item_id}")
async def read_item(item_id: int):
    return {"id": item_id}
```

`@app.get`登记GET路径；`{item_id}`提取路径参数；`int`让框架转换和校验输入。访问`/items/abc`会产生参数校验错误，不会把字符串默默当作编号。`demo:app`表示导入`demo`模块里的`app`对象。

OpenAPI是机器可读的接口说明，记录路径、参数、请求和响应Schema；FastAPI从类型和路由生成它。`/openapi.json`可供检查接口契约，交互页面只是读取契约的一种工具。HTTP 202表示已受理，后台任务是否成功仍需查询Job。

CORS是浏览器对跨源请求的访问控制。源由协议、主机和端口组成；同一主机的不同端口也可能跨源。允许一个源访问不等于验证用户身份，服务端仍须校验Bearer令牌及权限。

**在本项目中。** [main.py](../../app/main.py)装配应用、生命周期与路由，业务入口位于各模块`api/router.py`。主API使用Python 3.14；CPU模型服务是独立HTTP进程。继续读[第1章](../第01章_项目技术与功能模块介绍.md)和[第2章](../第02章_公共基座搭建启动与测试.md)。

<a id="t03"></a>
## 03 Pydantic、pydantic-settings与JSON Schema

**解决的问题。** 收到JSON不意味着字段齐全、类型正确或数值合理。Pydantic把外部数据转换成明确的对象，并按字段规则校验；校验通过只说明结构符合要求，还需要业务权限、状态和来源检查。

```python
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    pressure: float = Field(ge=0)


item = Measurement.model_validate({"name": "测试点", "pressure": 6.0})
assert item.pressure == 6.0
try:
    Measurement.model_validate({"name": "测试点", "pressure": -1})
except ValidationError:
    print("压力不符合输入约束")
```

`BaseModel`声明数据结构，`Field`附加范围限制，`extra="forbid"`拒绝未声明字段。`model_validate`接收普通数据，`model_dump(mode="json")`生成适合JSON编码的值。JSON Schema描述这些字段与约束，可供API文档及模型结构化输出使用；它不是实际数据，也不是数据库建表语句。

pydantic-settings在同样的类型规则上增加环境配置读取。例如`Settings`将环境变量和`.env`中的字符串解析为整数、布尔值或密钥对象。`SecretStr`帮助避免常规展示直接暴露密钥；业务代码确实需要认证值时才显式取出。

**在本项目中。** [config.py](../../app/bootstrap/config.py)管理配置，各模块`domain/schemas.py`管理接口数据，[ports/models.py](../../app/ports/models.py)约束模型输出。不能把“模型生成了合法JSON”理解成“内容已经正确”；现场事实和审批仍由业务规则与人工确认负责。

<a id="t15"></a>
## 15 httpx、HTTP/JSON、Provider、端口、依赖注入与适配器

**先认识调用边界。** HTTP客户端把请求发送给另一个服务；JSON是一种数据表达格式。httpx提供同步和异步客户端，异步版本允许请求等待期间让其他任务运行。

下面的函数演示读取本机就绪接口。它定义调用方式，执行时还需要在已有异步流程中调用它：

```python
import httpx


async def read_readiness():
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get("http://127.0.0.1:8000/health/ready")
        response.raise_for_status()
        return response.json()
```

`async with`保证连接资源关闭；超时限制等待；`raise_for_status`处理HTTP错误；`json()`只解析正文，不保证其业务含义。重定向、重试和代理配置也会影响请求行为，不能把所有错误都变成“没有结果”。

Provider表示一种能力的提供者。端口声明业务需要什么接口，适配器负责把它翻译成具体服务的协议：

```text
应用服务 → 文字生成端口 → Qwen HTTP适配器 → 百炼
测试     → 同一端口     → Fake适配器
```

依赖注入表示由装配位置提供数据库会话或Provider，业务函数无需自己寻找全局对象或重复创建连接。它能让生产使用真实服务、测试替换为可控实现；替换实现仍须满足相同输入输出契约。

**在本项目中。** 先读[文字端口](../../app/ports/text.py)、[能力装配](../../app/bootstrap/providers.py)和[Qwen适配器](../../app/infrastructure/providers/qwen_text.py)。[气象与空间端口](../../app/ports/site_context.py)采用同一结构。业务模块通过端口调用能力，保持协议细节集中在适配器中。
