# kernel 包

# 本包包含Agent的内核模块。

# 模块

# - supervisor.py: 管理Agent的启动和状态
# - version_manager.py: 管理版本和candidate机制
# - health_check.py: 检查系统健康状态
# - rollback.py: 处理版本回滚

# 使用

# ```python
# from kernel.supervisor import Supervisor
# from kernel.version_manager import VersionManager
# from kernel.health_check import HealthCheck
# from kernel.rollback import Rollback
# ```