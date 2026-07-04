#!/usr/bin/env python3
"""引导式添加 LLM 模型配置到 config.yaml。

交互层复用项目自带的 ``scripts/wizard/ui.py``（纯 stdlib 实现，无需 prompt_toolkit / rich），
与 ``setup_wizard.py`` 保持一致。api_key / base_url 可写入：

- 项目 ``.env``          —— DeerFlow 运行时实际加载的地方；
- ``~/.deerflow_env``    —— shell 全局变量文件（``export KEY=...``，权限 600），
                            并在 ``~/.zshrc`` 中确保一行 ``source ~/.deerflow_env``。

用法::

    uv run python scripts/add-model.py
    uv run python scripts/add-model.py --config path/to/config.yaml --env path/to/.env
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path

import yaml

# 让 scripts/ 可被 import，从而复用 wizard.* 包（与 setup_wizard.py 相同做法）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wizard.ui import (  # noqa: E402  (需在 sys.path 调整之后导入)
    ask_choice,
    ask_multi_choice,
    ask_secret,
    ask_text,
    ask_yes_no,
    bold,
    cyan,
    green,
    print_header,
    print_info,
    print_section,
    print_success,
    print_warning,
    red,
)

# ── 路径常量 ─────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
DEFAULT_ENV_PATH = ROOT / ".env"
# shell 全局变量落地文件与其宿主 rc；用 ~ 解析，绝不硬编码用户名
DEFAULT_SHELL_ENV_PATH = Path.home() / ".deerflow_env"
DEFAULT_ZSHRC_PATH = Path.home() / ".zshrc"

# 合法环境变量名：字母/下划线开头，其后字母/数字/下划线
_VAR_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


# ── 预设 provider 模板 ──────────────────────────────────────────────
PROVIDERS = [
    {
        "name": "OpenAI 兼容",
        "desc": "适用于大部分兼容 OpenAI API 的服务（Qwen、DeepSeek 等）",
        "use": "langchain_openai:ChatOpenAI",
        "defaults": {"request_timeout": 600.0, "max_retries": 2},
    },
    {
        "name": "DeepSeek",
        "desc": "DeepSeek 官方 API，含 reasoning 支持",
        "use": "langchain_openai:ChatOpenAI",
        "defaults": {"base_url": "https://api.deepseek.com/v1", "request_timeout": 600.0, "max_retries": 2},
    },
    {
        "name": "阿里云百炼 / Coding Plan (Qwen)",
        "desc": "通义千问，阿里 Coding Plan 端点",
        "use": "langchain_openai:ChatOpenAI",
        "defaults": {"base_url": "https://coding.dashscope.aliyuncs.com/v1", "request_timeout": 600.0, "max_retries": 2},
    },
    {
        "name": "火山引擎 (Doubao)",
        "desc": "豆包模型，含 reasoning 支持",
        "use": "deerflow.models.patched_deepseek:PatchedChatDeepSeek",
        "defaults": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "request_timeout": 600.0, "max_retries": 2},
    },
    {
        "name": "Ollama (本地)",
        "desc": "本地运行的 Ollama 模型",
        "use": "langchain_ollama:ChatOllama",
        "defaults": {"base_url": "http://localhost:11434", "temperature": 0.7},
    },
    {
        "name": "Anthropic Claude",
        "desc": "Claude Sonnet / Opus，含 extended thinking",
        "use": "langchain_anthropic:ChatAnthropic",
        "defaults": {"default_request_timeout": 600.0, "max_retries": 2, "max_tokens": 16000},
    },
    {
        "name": "Google Gemini",
        "desc": "Gemini 2.5 Pro / Flash 等",
        "use": "langchain_google_genai:ChatGoogleGenerativeAI",
        "defaults": {"timeout": 600.0, "max_retries": 2, "max_tokens": 8192},
    },
    {
        "name": "自定义",
        "desc": "手动输入 LangChain use 路径",
        "use": None,
        "defaults": {},
    },
]


# ── 通用小工具 ───────────────────────────────────────────────────────

def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    """原子写入：先写同目录临时文件，再 ``os.replace`` 覆盖，避免写一半损坏文件。

    ``mode`` 显式指定则用之（如密钥文件 0o600）；否则沿用已存在文件的权限，
    新文件默认 0o644。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证 os.replace 在同一文件系统上是原子操作
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        # 任何失败都清理临时文件，避免残留
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tilde(path: Path) -> str:
    """把 home 目录下的路径显示为 ``~/xxx``，其余原样返回。"""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _shell_single_quote(value: str) -> str:
    """把任意字符串安全地包成 POSIX 单引号形式，供 shell ``export`` 使用。"""
    return "'" + value.replace("'", "'\\''") + "'"


def _valid_var_name(name: str) -> bool:
    """校验环境变量名是否合法。"""
    return bool(_VAR_NAME_RE.match(name))


def _maybe_set(entry: dict, key: str, raw: str, caster) -> None:
    """把非空字符串按 ``caster`` 转换后写入 entry；转换失败则忽略该字段。"""
    if not raw:
        return
    try:
        entry[key] = caster(raw)
    except ValueError:
        pass


# ── .env 读写 ────────────────────────────────────────────────────────

def read_env_vars(env_path: Path) -> dict[str, str]:
    """读取 .env 中的 ``KEY=VALUE``（忽略注释与空行）。"""
    if not env_path.exists():
        return {}
    envs: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            envs[m.group(1)] = m.group(2)
    return envs


def write_env_var(env_path: Path, key: str, value: str) -> None:
    """把 ``KEY=value`` 幂等写入 .env（已存在则替换，否则追加）。"""
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        replaced = False
        for line in lines:
            if re.match(rf"^{re.escape(key)}=(.*)$", line.strip()):
                new_lines.append(f"{key}={value}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append("")
            new_lines.append(f"# {key}")
            new_lines.append(f"{key}={value}")
        _atomic_write(env_path, "\n".join(new_lines) + "\n")
    else:
        _atomic_write(env_path, f"{key}={value}\n")


# ── shell 全局变量（~/.deerflow_env + ~/.zshrc） ─────────────────────

def write_shell_export(shell_env_path: Path, key: str, value: str) -> None:
    """把 ``export KEY=value`` 幂等写入 shell 全局变量文件，权限收紧为 600。"""
    line = f"export {key}={_shell_single_quote(value)}"
    if shell_env_path.exists():
        lines = shell_env_path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        replaced = False
        pattern = re.compile(rf"^\s*export\s+{re.escape(key)}=")
        for existing in lines:
            if pattern.match(existing):
                new_lines.append(line)
                replaced = True
            else:
                new_lines.append(existing)
        if not replaced:
            new_lines.append(line)
        text = "\n".join(new_lines) + "\n"
    else:
        text = "# DeerFlow 全局变量（由 scripts/add-model.py 写入，建议 gitignore）\n" + line + "\n"
    _atomic_write(shell_env_path, text, mode=0o600)


def ensure_zshrc_sources(zshrc_path: Path, shell_env_path: Path) -> bool:
    """确保 ~/.zshrc 内有一行 ``source <shell_env_path>``；已存在则不动。

    返回是否新增了该行。
    """
    display = _tilde(shell_env_path)
    abs_path = str(shell_env_path)
    existing_text = zshrc_path.read_text(encoding="utf-8") if zshrc_path.exists() else ""

    for line in existing_text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("source ") or stripped.startswith(". ")):
            continue
        target = stripped.split(None, 1)[1].strip()
        if os.path.expanduser(target) == abs_path or target == display:
            return False  # 已经 source 过

    block = ""
    if existing_text and not existing_text.endswith("\n"):
        block += "\n"
    block += f"\n# DeerFlow 全局变量\nsource {display}\n"
    _atomic_write(zshrc_path, existing_text + block)
    return True


# ── config.yaml 读取与追加 ───────────────────────────────────────────

def existing_model_names(config_path: Path) -> set[str]:
    """只读解析 config.yaml，收集已存在的 ``models[].name``。

    解析失败时返回空集合（不阻塞主流程），交由后续写入环节处理。
    """
    if not config_path.exists():
        return set()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return set()
    names: set[str] = set()
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        for item in data["models"]:
            if isinstance(item, dict) and "name" in item:
                names.add(str(item["name"]))
    return names


def append_model_to_config(config_path: Path, model_entry: dict) -> None:
    """把 model_entry 追加到 config.yaml 的 ``models:`` 列表，尽量保留原格式与注释。"""
    dumped = yaml.dump([model_entry], allow_unicode=True, default_flow_style=False, sort_keys=False)

    # 文件不存在：直接新建一个含 models: 的文件
    if not config_path.exists():
        _atomic_write(config_path, "models:\n" + textwrap.indent(dumped, "  ").rstrip() + "\n")
        return

    raw = config_path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    # 定位 models: 顶级键
    models_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "models:":
            models_idx = i
            break

    # 没有 models: 段落：在文件末尾追加一段
    if models_idx is None:
        entry_yaml = textwrap.indent(dumped, "  ").rstrip()
        _atomic_write(config_path, raw.rstrip("\n") + "\n\nmodels:\n" + entry_yaml + "\n")
        return

    models_indent = len(lines[models_idx]) - len(lines[models_idx].lstrip())
    entry_indent = models_indent + 2

    # 找到 models: 段落结束处（下一个同级或更浅的非注释行）
    insert_at = len(lines)
    for i in range(models_idx + 1, len(lines)):
        stripped = lines[i].rstrip()
        if stripped == "":
            continue
        line_indent = len(lines[i]) - len(lines[i].lstrip())
        if line_indent <= models_indent and not stripped.startswith("#"):
            insert_at = i
            break
        insert_at = i + 1

    entry_yaml = textwrap.indent(dumped, " " * entry_indent).rstrip()

    new_lines = list(lines)
    if insert_at < len(new_lines) and new_lines[insert_at - 1] != "":
        new_lines.insert(insert_at, "")
        insert_at += 1
    new_lines.insert(insert_at, entry_yaml)
    if insert_at + 1 < len(new_lines) and new_lines[insert_at + 1] != "":
        new_lines.insert(insert_at + 1, "")
    _atomic_write(config_path, "\n".join(new_lines) + "\n")


# ── 富文本输出 ─────────────────────────────────────────────────────────

def print_banner() -> None:
    """打印向导标题。"""
    print_header("DeerFlow 模型配置向导")
    print_info("引导式添加 LLM 模型 · 可选写入 .env / ~/.deerflow_env 全局变量")


def print_preview(model_entry: dict) -> None:
    """打印将写入 config.yaml 的模型配置预览。"""
    print_section("预览")
    width = max((len(k) for k in model_entry), default=0)
    for k, v in model_entry.items():
        shown = (green("✓") if v else red("✗")) if isinstance(v, bool) else str(v)
        print(f"  {k.ljust(width)}  {shown}")


# ── api_key 配置流程 ─────────────────────────────────────────────────

def configure_api_key(env_path: Path, step_label: str = "4/6") -> tuple[str, list[tuple[str, str]]]:
    """引导配置 api_key。返回 ``(写入 config 的值, 需落地的 [(变量名, 值)] 列表)``。"""
    print_section(f"Step {step_label} · API Key 配置")
    idx = ask_choice(
        "选择 api_key 来源",
        [
            "创建新的全局变量（写入 .env + ~/.deerflow_env）",
            "使用已有的 .env 变量",
            "直接填写明文（写入 config.yaml）",
            "跳过（暂不设置 api_key）",
        ],
    )

    # 新建全局变量
    if idx == 0:
        var_name = ask_text("变量名（如 DASHSCOPE_API_KEY）").strip().upper()
        if not var_name:
            print_warning("变量名不能为空，已跳过。")
            return "", []
        if not _valid_var_name(var_name):
            print_warning(f"变量名 {var_name} 不合法（仅字母/数字/下划线，且不以数字开头），已跳过。")
            return "", []
        value = ask_secret("变量值（输入不可见）")
        return f"${var_name}", [(var_name, value)]

    # 复用已有 .env 变量
    if idx == 1:
        envs = read_env_vars(env_path)
        if not envs:
            print_warning(".env 中尚无变量，回退到创建新变量。")
            return configure_api_key(env_path, step_label)
        names = sorted(envs.keys())
        sel = ask_choice("选择已有的 .env 变量", names)
        return f"${names[sel]}", []

    # 直接明文
    if idx == 2:
        return ask_secret("api_key（明文）"), []

    # 跳过
    return "", []


# ── base_url 配置流程 ────────────────────────────────────────────────

def configure_base_url(env_path: Path, default_url: str, step_label: str = "5/6") -> tuple[str, list[tuple[str, str]]]:
    """引导配置 base_url。返回 ``(写入 config 的值, 需落地的 [(变量名, 值)] 列表)``。"""
    print_section(f"Step {step_label} · Base URL 配置")
    if default_url:
        print_info(f"Provider 默认地址: {cyan(default_url)}")

    opts: list[str] = []
    if default_url:
        opts.append(f"使用默认值: {default_url}")
    opts += ["自定义 base_url", "不使用 base_url"]

    chosen = opts[ask_choice("base_url 设置", opts, default=0)]

    if chosen.startswith("使用默认值"):
        return default_url, []
    if chosen == "不使用 base_url":
        return "", []

    # 自定义 base_url
    url = ask_text("base_url（API 地址）", required=True)
    if not ask_yes_no("把此 URL 保存为全局变量（供其他模型复用）？", default=False):
        return url, []

    var_name = ask_text("新变量名（如 MY_BASE_URL）").strip().upper()
    if not var_name or not _valid_var_name(var_name):
        print_warning("变量名为空或不合法，将直接使用明文 URL。")
        return url, []
    if var_name in read_env_vars(env_path) and not ask_yes_no(
        f".env 中已有 {var_name}，覆盖为新值？", default=False
    ):
        print_warning("已取消保存，将直接使用明文 URL。")
        return url, []
    return f"${var_name}", [(var_name, url)]


# ── 组装 model_entry ─────────────────────────────────────────────────

def build_model_entry(
    *,
    name: str,
    display: str,
    use: str,
    model_id: str,
    api_key: str,
    base_url: str,
    timeout: str,
    max_retries: str,
    max_tokens: str,
    temperature: str,
    supports_vision: bool,
    supports_thinking: bool,
) -> dict:
    """把各步骤收集到的字段拼装成待写入 config.yaml 的字典（仅保留已填字段）。"""
    entry: dict = {"name": name, "display_name": display, "use": use, "model": model_id}
    if api_key:
        entry["api_key"] = api_key
    if base_url:
        entry["base_url"] = base_url
    _maybe_set(entry, "request_timeout", timeout, float)
    _maybe_set(entry, "max_retries", max_retries, int)
    _maybe_set(entry, "max_tokens", max_tokens, int)
    _maybe_set(entry, "temperature", temperature, float)
    if supports_vision:
        entry["supports_vision"] = True
    if supports_thinking:
        entry["supports_thinking"] = True
    return entry


# ── 命令行参数 ───────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="引导式添加 LLM 模型配置到 DeerFlow config.yaml。")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help=f"config.yaml 路径（默认 {DEFAULT_CONFIG_PATH}）")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH,
                        help=f".env 路径（默认 {DEFAULT_ENV_PATH}）")
    return parser.parse_args(argv)


# ── 主流程 ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """向导主流程；返回进程退出码（0 成功，非 0 表示取消/失败）。"""
    args = _parse_args(argv)
    config_path: Path = args.config
    env_path: Path = args.env
    shell_env_path = DEFAULT_SHELL_ENV_PATH
    zshrc_path = DEFAULT_ZSHRC_PATH

    print_banner()
    if not config_path.exists():
        print_warning(f"未找到 {config_path}，将新建该文件。")

    # ── Step 1: 名称 ──
    print_section("Step 1/6 · 模型标识")
    name = ask_text("模型名称（唯一标识，如 qwen-max）", required=True)
    display = ask_text("显示名称（UI 中展示的名字）", default=name)

    # 重名检测：命中则确认是否仍追加
    if name in existing_model_names(config_path):
        if not ask_yes_no(f"config.yaml 中已存在同名模型 {name}，仍要追加？", default=False):
            print_info("已取消，未写入。")
            return 0

    # ── Step 2: Provider ──
    print_section("Step 2/6 · 选择 Provider 类型")
    provider = PROVIDERS[ask_choice("选择 Provider", [f"{p['name']}  ({p['desc']})" for p in PROVIDERS])]
    if provider["use"] is None:
        use = ask_text("自定义 use 路径（如 langchain_openai:ChatOpenAI）", required=True)
    else:
        use = provider["use"]

    # ── Step 3: 模型参数 ──
    print_section("Step 3/6 · 模型参数")
    defaults = provider["defaults"]
    model_id = ask_text("model（API 调用的模型名）", default=name)
    timeout = ask_text("请求超时秒数") or str(
        defaults.get("request_timeout")
        or defaults.get("timeout")
        or defaults.get("default_request_timeout")
        or ""
    )
    max_retries = ask_text("最大重试次数") or str(defaults.get("max_retries", ""))
    max_tokens = ask_text("最大输出 token") or str(defaults.get("max_tokens", ""))
    temperature = ask_text("temperature") or str(defaults.get("temperature", ""))

    # ── Step 4 / 5: api_key & base_url ──
    api_key, env_writes_api = configure_api_key(env_path)
    base_url, env_writes_base = configure_base_url(env_path, defaults.get("base_url", ""))

    # ── Step 6: 可选能力 ──
    print_section("Step 6/6 · 启用能力")
    caps = ask_multi_choice("选择要启用的能力（可多选，留空跳过）", ["支持视觉 (Vision)", "支持思考模式 (Thinking)"])

    model_entry = build_model_entry(
        name=name,
        display=display,
        use=use,
        model_id=model_id,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
        max_tokens=max_tokens,
        temperature=temperature,
        supports_vision=0 in caps,
        supports_thinking=1 in caps,
    )

    # ── 预览 & 确认 ──
    print_preview(model_entry)

    # 合并两处需要落地的全局变量（后者覆盖前者的同名项）
    env_writes: dict[str, str] = {}
    for k, v in env_writes_api + env_writes_base:
        env_writes[k] = v

    if not ask_yes_no("确认写入 config.yaml？", default=True):
        print_info("已取消，未写入。")
        return 0

    # 写入全局变量：.env + ~/.deerflow_env，并确保 ~/.zshrc source 它
    zshrc_updated = False
    for k, v in env_writes.items():
        write_env_var(env_path, k, v)
        write_shell_export(shell_env_path, k, v)
    if env_writes:
        zshrc_updated = ensure_zshrc_sources(zshrc_path, shell_env_path)

    append_model_to_config(config_path, model_entry)

    # ── 结果汇报 ──
    print()
    print_success(f"模型 {bold(name)} 已添加到 {config_path}")
    if env_writes:
        print_success(f"已写入 .env 与 {_tilde(shell_env_path)}: {', '.join(env_writes)}")
        if zshrc_updated:
            print_info(f"已在 {_tilde(zshrc_path)} 添加 source {_tilde(shell_env_path)}")
        print()
        print_info("让全局变量在当前终端生效，请执行（或直接新开终端）：")
        print(f"    source {_tilde(zshrc_path)}")
    if api_key.startswith("$"):
        print_info(f"api_key 引用全局变量 {cyan(api_key)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        print_warning("已取消（Ctrl-C）。")
        raise SystemExit(130)
    except EOFError:
        print()
        print_warning("输入结束，已取消。")
        raise SystemExit(130)
