"""确定性代码安检、三层模糊补丁引擎与 JS 静态/语法校验（全部零 Token）。

Reviewer 的确定性防线分三层：
1. PatchAndSanitizerEngine：思维链剥离、SEARCH/REPLACE 三层模糊合并、HTML 完整性/防缩水门禁；
2. static_js_issues：静态规则（禁用 alert/prompt/confirm、有交互控件却无脚本等）；
3. check_js_syntax：提取 <script> 经本机 `node --check` 做语法校验（无 node 环境自动跳过）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap

from .common import _CODE_MIN_LEN, _NODE_CHECK_TIMEOUT, logger


class PatchAndSanitizerEngine:
    """零 Token 确定性合并与安检门禁：
    1. 剥离 <think>...</think> 思维链泄漏与 Markdown 代码块围栏；
    2. 三层渐进式 SEARCH/REPLACE 模糊替换（精确 -> 行尾空白归一化 -> 块级缩进归一化）；
       SEARCH 出现多次时视为歧义失败并给出可读错误（供 Reviewer 修正回路反馈 Coder）；
    3. HTML 完整性门禁（成对标签闭合 + PATCH 后防缩水校验）。
    """

    PATCH_BLOCK_RE = re.compile(
        r"^<{5,9}\s*SEARCH\s*\r?\n(.*?)\r?\n={5,9}\s*\r?\n(.*?)\r?\n>{5,9}\s*REPLACE",
        re.MULTILINE | re.DOTALL,
    )

    @staticmethod
    def strip_thinking_and_fences(raw_text: str) -> str:
        if not raw_text:
            return ""
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.IGNORECASE).strip()
        fences = re.findall(r"```(?:html|htm|xml)?\s*\r?\n([\s\S]*?)```", cleaned, flags=re.IGNORECASE)
        if fences:
            cleaned = max(fences, key=len).strip()
        m_start = re.search(r"(<!DOCTYPE\s+html[^>]*>|<html\b[^>]*>)", cleaned, flags=re.IGNORECASE)
        m_end = list(re.finditer(r"</html>", cleaned, flags=re.IGNORECASE))
        if m_start and m_end:
            cleaned = cleaned[m_start.start(): m_end[-1].end()].strip()
        return cleaned

    @classmethod
    def extract_patch_blocks(cls, raw_text: str) -> list[tuple[str, str]]:
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text or "", flags=re.IGNORECASE)
        blocks: list[tuple[str, str]] = []
        for m in cls.PATCH_BLOCK_RE.finditer(cleaned):
            search_part, replace_part = m.group(1), m.group(2)
            if search_part.strip():
                blocks.append((search_part, replace_part))
        return blocks

    @staticmethod
    def _fuzzy_replace_single(code: str, search_block: str, replace_block: str) -> tuple[bool, str, str | None]:
        """返回 (是否成功, 新代码, 错误信息)。Tier 1 精确匹配且要求唯一。"""
        # Tier 1: 精确匹配（SEARCH 必须在原代码中唯一存在）
        count = code.count(search_block)
        if count == 1:
            return True, code.replace(search_block, replace_block, 1), None
        if count > 1:
            return False, code, f"SEARCH 片段在现有代码中出现 {count} 次，无法唯一定位（请扩大上下文行数）"

        code_lines = code.splitlines(keepends=True)
        search_lines = search_block.splitlines()
        n = len(search_lines)
        if n == 0 or n > len(code_lines):
            return False, code, "SEARCH 片段在原代码中未找到（需与原文逐字一致，注意缩进与空行）"

        # Tier 2: 行尾空白归一化匹配
        search_rstrip = [ln.rstrip() for ln in search_lines]
        for i in range(len(code_lines) - n + 1):
            window = [ln.rstrip() for ln in code_lines[i: i + n]]
            if window == search_rstrip:
                rep_text = replace_block if replace_block.endswith("\n") else replace_block + "\n"
                new_code = "".join(code_lines[:i]) + rep_text + "".join(code_lines[i + n:])
                return True, new_code, None

        # Tier 3: 块级缩进偏移归一化匹配（textwrap.dedent）
        dedented_search = textwrap.dedent("\n".join(search_lines)).strip()
        if not dedented_search:
            return False, code, "SEARCH 片段为空"
        for i in range(len(code_lines) - n + 1):
            raw_window = "".join(code_lines[i: i + n])
            if textwrap.dedent(raw_window).strip() == dedented_search:
                non_empty = [ln for ln in code_lines[i: i + n] if ln.strip()]
                base_indent = ""
                if non_empty:
                    min_spaces = min(len(ln) - len(ln.lstrip(" \t")) for ln in non_empty)
                    base_indent = non_empty[0][:min_spaces]
                dedented_rep = textwrap.dedent(replace_block)
                reindented_rep = textwrap.indent(dedented_rep, base_indent)
                if not reindented_rep.endswith("\n"):
                    reindented_rep += "\n"
                new_code = "".join(code_lines[:i]) + reindented_rep + "".join(code_lines[i + n:])
                return True, new_code, None

        return False, code, "SEARCH 片段在原代码中未找到（需与原文逐字一致，注意缩进与空行）"

    @classmethod
    def apply_patches(cls, code: str, blocks: list[tuple[str, str]]) -> tuple[str, list[str]]:
        """依次合并全部补丁并收集错误；任一失败即返回原始 code 与错误列表（供修正回路）。"""
        if not blocks:
            return code, ["未解析到有效 SEARCH/REPLACE 补丁块"]
        new_code = code
        errors: list[str] = []
        for idx, (search_part, replace_part) in enumerate(blocks, start=1):
            ok, new_code, err = cls._fuzzy_replace_single(new_code, search_part, replace_part)
            if not ok:
                errors.append(f"补丁 {idx}/{len(blocks)}：{err}")
        if errors:
            return code, errors
        if new_code == code:
            return code, ["补丁未对代码产生任何改动"]
        return new_code, []

    @staticmethod
    def validate_html(html: str, base_code: str | None = None, is_patch: bool = False) -> tuple[bool, str]:
        if not html or len(html.strip()) < _CODE_MIN_LEN:
            return False, "HTML 内容过短或为空"
        low = html.lower()
        if "<html" not in low or "</html>" not in low:
            return False, "缺少完整的 <html>...</html> 闭合结构"
        if "<body" in low and "</body>" not in low:
            return False, "缺少 </body> 闭合标签，疑似流式输出截断"
        if "<script" in low and "</script>" not in low:
            return False, "缺少 </script> 闭合标签，脚本不完整"
        if is_patch and base_code and len(html) < len(base_code) * 0.55:
            return False, "PATCH 后代码长度异常缩水超过 45%，疑似触发省略号破坏"
        return True, "OK"


# ==============================================================================
# JS 确定性校验：静态规则 + node --check 语法校验
# ==============================================================================
_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", re.IGNORECASE)
_BANNED_JS_CALL_RE = re.compile(r"\b(alert|prompt|confirm)\s*\(")
_INTERACTIVE_RE = re.compile(r"<button|<input[^>]*(?:onclick|oninput)|onclick=", re.IGNORECASE)


def extract_scripts(html: str | None) -> list[str]:
    """提取全部内联 <script> 内容（跳过 src 外链脚本）。"""
    if not html:
        return []
    return [m.group(1) for m in _SCRIPT_RE.finditer(html)]


def static_js_issues(html: str | None) -> list[str]:
    """零依赖静态规则检查，返回可读问题列表（无问题返回空列表）。"""
    issues: list[str] = []
    if not html:
        return ["HTML 为空"]
    scripts = extract_scripts(html)
    joined = "\n".join(scripts)
    for m in _BANNED_JS_CALL_RE.finditer(joined):
        line_no = joined.count("\n", 0, m.start()) + 1
        issues.append(f"脚本第 {line_no} 行使用了阻塞沙盒的 {m.group(1)}()，请改为页面内提示")
    if _INTERACTIVE_RE.search(html) and not scripts:
        issues.append("页面存在可交互控件（按钮/输入框/onclick）但没有任何内联 <script>，控件将是摆设")
    return issues


def check_js_syntax(html: str | None) -> list[str]:
    """提取内联脚本经本机 `node --check` 做语法校验；无 node 环境自动跳过（返回空列表）。"""
    if shutil.which("node") is None:
        return []
    issues: list[str] = []
    for idx, script in enumerate(extract_scripts(html), start=1):
        code = script.strip()
        if not code:
            continue
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                fh.write(code)
                tmp_path = fh.name
            proc = subprocess.run(
                ["node", "--check", tmp_path],
                capture_output=True, text=True, timeout=_NODE_CHECK_TIMEOUT,
            )
            if proc.returncode != 0:
                # node --check 输出形如：...SyntaxError: ... (line:NN)
                first_err = next((ln for ln in proc.stderr.splitlines() if "Error" in ln), proc.stderr.strip())
                issues.append(f"<script> 块 {idx} 语法错误：{first_err.strip()}")
        except subprocess.TimeoutExpired:
            issues.append(f"<script> 块 {idx} 语法校验超时（>{_NODE_CHECK_TIMEOUT}s）")
        except Exception as exc:  # noqa: BLE001
            logger.warning("node --check 执行失败，跳过语法校验: %s", exc)
        finally:
            try:
                os.unlink(tmp_path)  # type: ignore[possibly-undefined]
            except OSError:
                pass
    return issues


def post_generation_issues(html: str | None) -> list[str]:
    """Reviewer 的完整确定性体检 = 静态规则 + 语法校验。"""
    return static_js_issues(html) + check_js_syntax(html)
