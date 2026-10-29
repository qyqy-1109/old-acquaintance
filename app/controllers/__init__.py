"""Controller 层：HTTP 的进出口。

只做三件事：校验入参、调 Service、把结果包成响应。
业务判断一律不写在这里，否则 Web 和 CLI 会出现两套行为。
"""
