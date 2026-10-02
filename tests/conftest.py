"""测试全局夹具。

只有一件事：**每条测试前后都清掉配置缓存**。

`get_settings()` 带 lru_cache（配置在一个进程里是常量，不该每次调用都重新解析），
代价是"改了环境变量必须显式失效"。忘了失效的表现极隐蔽：测试里
`monkeypatch.setenv("DATABASE_PATH", ...)` 之后，被测代码读到的还是**旧值** ——
于是测试失败，而你会去翻被测代码（它是无辜的）。

把它做成 autouse 夹具 = 让"忘记失效"这件事在结构上不可能发生，
而不是靠每个人记得写一行 `reset_settings_cache()`。
"""

import pytest

from app.config import reset_settings_cache


@pytest.fixture(autouse=True)
def fresh_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()
