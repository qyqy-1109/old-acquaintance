"""Model 层：数据怎么存、怎么检索、按什么规则演化。

    personality.py  我是谁（五个性格维度 + 知识边界）
    world.py        你和我共同生活的世界长什么样（可手写，不遗忘）
    user.py         用户是谁（由分析模型独立提炼的画像）
    memory.py       我们聊过什么（事实短句 + 滚动摘要 + 会遗忘的向量记忆）

这一层不知道自己被谁调用：Web、CLI、脚本都可以，它只管读写正确。
"""

from app.models.memory import MemoryStore
from app.models.personality import Personality
from app.models.user import UserProfile
from app.models.world import WorldBook

__all__ = ["MemoryStore", "Personality", "UserProfile", "WorldBook"]
