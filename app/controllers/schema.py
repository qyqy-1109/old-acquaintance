from pydantic import BaseModel, Field, field_validator


class PromiseIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=60)
    due_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("承诺不能为空")
        return clean


class PromiseCompleteIn(BaseModel):
    note: str = Field(default="", max_length=120)


class PromiseMissIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=120)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("失约需要填写原因")
        return clean


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, description="用户这一轮说的话")


class CommandIn(BaseModel):
    command: str = Field(..., min_length=1, description="以 / 开头的命令，如 /mem")


class WorldIn(BaseModel):
    text: str = Field(..., min_length=1, description="世界书条目，一句一个设定")


class AckIn(BaseModel):
    """确认哪几条待发表已经**显示给用户**了（`#119` 的至少一次投递协议）。"""
    ids: list[int] = Field(default_factory=list, description="`pending_say.id` 列表")


class PinIn(BaseModel):
    """看板「钉进她的牵挂」的内容（`T91`）：一句话，落进 `life.json` 的 `cares`。"""
    text: str = Field(..., min_length=1, max_length=60, description="要钉进她编剧层的一句话")
