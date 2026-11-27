"""voicekit：把一个作者的文风提炼成可数的判据，再拿去体检或起稿。

组成：
- `voicekit.core`：引擎（42 条可数属性 + 作者包 + 生成/改稿/判官）
- `voicekit.cli`：命令行 `lint` / `report` / `build` / `draft`
- `voicekit.compat`：标准库 LLM 适配层（OpenAI 兼容接口）
"""

__version__ = "0.1.12"
