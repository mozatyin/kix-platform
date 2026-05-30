"""i18n seed: populate zh-Hans-SG catalog from existing in-source Chinese.

The codebase already contains high-quality manual Chinese translations
in three patterns:

    1. ``tutorials.py``     — ``MODULE_META[k] = {"name_en":..., "name_cn":...}``
    2. ``tutorials.py``     — ``INSTRUCTION_TEMPLATES[k] = {"cn":..., "en":...}``
    3. ``conditions.py``    — ``FIX_HINTS[code] = {"zh":..., "en":...}``
    4. ``welcome_kit.py``   — ``_ITEMS[k] = {"title": "<Chinese>", "description": "<Chinese>"}``

This script harvests those mappings and emits a Fluent catalog at
``app/i18n/catalogs/zh-Hans-SG/main.ftl`` so the en-SG keys we just
shipped have real human-quality zh-Hans translations on day one.

When the LLM translator runs later (``scripts.i18n_translate``) the keys
already populated here will hit the translation memory and be skipped,
saving cost.

Run:
    .venv/bin/python -m scripts.i18n_seed_zh_from_source
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("i18n_seed_zh")


def _load_module_meta() -> dict[str, str]:
    """tutorials-module-<id> → name_cn."""
    src = (REPO_ROOT / "app/routers/tutorials.py").read_text()
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "MODULE_META"
            and node.value is not None
        ):
            obj = ast.literal_eval(node.value)
            for module_id, meta in obj.items():
                if "name_cn" in meta:
                    out[f"tutorials-module-{module_id}"] = meta["name_cn"]
    return out


def _load_instruction_templates() -> dict[str, str]:
    """tutorials-step-<key> → cn template (with {var} → { $var })."""
    src = (REPO_ROOT / "app/routers/tutorials.py").read_text()
    tree = ast.parse(src)
    out: dict[str, str] = {}
    SLUG_OVERRIDE = {
        "intro": "intro",
        "navigate_engagement": "navigate-engagement",
        "navigate_vouchers": "navigate-vouchers",
        "navigate_rules": "navigate-rules",
        "enable_module": "enable-module",
        "configure_module": "configure-module",
        "create_voucher_template": "create-voucher-template",
        "create_rule": "create-rule",
        "test_action": "test-action",
        "celebrate": "celebrate",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "INSTRUCTION_TEMPLATES"
            and node.value is not None
        ):
            obj = ast.literal_eval(node.value)
            for key, tpl in obj.items():
                slug = SLUG_OVERRIDE.get(key, key.replace("_", "-"))
                cn = tpl.get("cn")
                if cn:
                    out[f"tutorials-step-{slug}"] = _python_fmt_to_fluent(cn)
    return out


def _load_fix_hints() -> dict[str, str]:
    """conditions-blocker-<code> → zh."""
    src = (REPO_ROOT / "app/routers/conditions.py").read_text()
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "FIX_HINTS"
            and node.value is not None
        ):
            obj = ast.literal_eval(node.value)
            for code, entry in obj.items():
                if "zh" in entry:
                    out[f"conditions-blocker-{code}"] = entry["zh"]
    return out


def _load_welcome_kit_items() -> dict[str, str]:
    """welcome_kit-item-<key>-{title,desc} → Chinese strings."""
    src = (REPO_ROOT / "app/routers/welcome_kit.py").read_text()
    tree = ast.parse(src)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_ITEMS"
            and node.value is not None
        ):
            obj = ast.literal_eval(node.value)
            for key, entry in obj.items():
                if "title" in entry:
                    out[f"welcome_kit-item-{key}-title"] = entry["title"]
                if "description" in entry:
                    out[f"welcome_kit-item-{key}-desc"] = entry["description"]
    out["welcome_kit-default-tagline"] = "扫码玩游戏 拿奖励！"
    return out


def _python_fmt_to_fluent(s: str) -> str:
    """Convert ``"{foo}"`` → ``"{ $foo }"`` for Fluent."""
    import re
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", r"{ $\1 }", s)


# Manually-curated translations for keys without an in-source CN equivalent.
EXTRA_TRANSLATIONS: dict[str, str] = {
    # recipe_generator
    "recipe_generator-match-found": "已从配方库匹配现成方案 '{ $recipe_name }'。",
    "recipe_generator-match-score": "匹配分数 { $score }，原因：{ $reasons }。",
    "recipe_generator-summary-untitled": "未命名",
    "recipe_generator-summary-empty-modules": "无",
    "recipe_generator-summary-recipe-includes": (
        "配方 '{ $recipe_name }' 包含 { $module_count } 个模块："
        "{ $module_list }，通过 { $rule_count } 条规则连接。"
    ),
    "recipe_generator-heuristic-fallback": "（启发式模板）根据关键词匹配选择了相关模块和默认规则。",
    "recipe_generator-default-description": "邀请10位好友，解锁免费咖啡券",

    # modules.py
    "modules-status-active": "已启用",
    "modules-status-inactive": "未启用",
    "modules-status-coming_soon": "即将上线",
    "modules-action-enable": "启用",
    "modules-action-disable": "停用",
    "modules-action-configure": "配置",

    # API errors
    "error-internal": "服务器内部错误，请稍后重试。",
    "error-not_found": "找不到该资源。",
    "error-unauthorized": "需要登录。",
    "error-forbidden": "您没有权限执行该操作。",
    "error-validation": "请求参数校验失败。",
    "error-rate_limited": "请求过于频繁，请稍后再试。",
    "error-conflict": "请求与当前资源状态冲突。",

    # Common UI
    "common-cta-login": "登录",
    "common-cta-logout": "退出",
    "common-cta-signup": "注册",
    "common-cta-cancel": "取消",
    "common-cta-save": "保存",
    "common-cta-confirm": "确认",
    "common-cta-back": "返回",
    "common-cta-next": "下一步",
    "common-cta-loading": "加载中…",
    "common-nav-home": "首页",
    "common-nav-portal": "管理端",
    "common-nav-storefront": "店铺",
    "common-nav-play": "玩",
    "common-nav-connect": "连接",
    "common-currency-sgd": "新元",
    "common-currency-cny": "人民币",
    "common-currency-usd": "美元",

    # Smoke-test message (existing)
    "welcome-message": "欢迎 { $name }！",
    "welcome-message.description": (
        "您有 { $count } 条消息"
    ),
}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    all_translations: dict[str, str] = {}
    all_translations.update(_load_module_meta())
    all_translations.update(_load_instruction_templates())
    all_translations.update(_load_fix_hints())
    all_translations.update(_load_welcome_kit_items())
    all_translations.update(EXTRA_TRANSLATIONS)

    logger.info("Harvested %d zh-Hans-SG translations", len(all_translations))

    # Read the en-SG catalog for structure/ordering
    en_catalog = REPO_ROOT / "app/i18n/catalogs/en-SG/main.ftl"
    en_src = en_catalog.read_text().splitlines()

    out_lines: list[str] = []
    out_lines.append("### KiX Platform — Simplified Chinese (Singapore) catalog")
    out_lines.append("### Seeded from in-source translations + curated extras.")
    out_lines.append("### Source-of-truth: app/i18n/catalogs/en-SG/main.ftl")
    out_lines.append("")

    i = 0
    pending_key: str | None = None
    pending_indent = ""
    used_keys: set[str] = set()
    in_message = False
    skip_attribute = False

    # Simpler approach: re-parse the en-SG file line-by-line; for every
    # ``key = ...`` we emit ``key = <zh>`` if a translation exists.
    skipped_keys: list[str] = []
    out_lines = []
    out_lines.append("### KiX Platform — Simplified Chinese (Singapore) catalog (Wave 2)")
    out_lines.append("### Seeded from in-source translations (tutorials/conditions/welcome_kit)")
    out_lines.append("### + curated extras for recipe_generator / modules / errors / common UI.")
    out_lines.append("")

    lines = en_src
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # Pass through comments / blanks
        if not stripped or stripped.startswith("#") or stripped.startswith("###"):
            out_lines.append(line)
            i += 1
            continue
        # Identifier-starting message line: "<key> = <value>"
        if "=" in line and not line.startswith(" ") and not line.startswith("\t"):
            key, _, _ = line.partition("=")
            key = key.strip()
            if key in all_translations:
                out_lines.append(f"{key} = {all_translations[key]}")
                used_keys.add(key)
                # Skip continuation indented attribute lines belonging to this message
                # (we drop ICU plural attrs for simplicity in zh-Hans — Chinese is
                # always "other" plural category)
                j = i + 1
                while j < n and (lines[j].startswith("    ") or lines[j].startswith("\t")):
                    j += 1
                # If this message had a .description attribute, emit the seeded one.
                attr_key = f"{key}.description"
                if attr_key in all_translations:
                    out_lines.append(f"    .description = {all_translations[attr_key]}")
                i = j
                continue
            else:
                skipped_keys.append(key)
                # Keep the English line so the file is valid Fluent and
                # the missing key still resolves via fallback.
                out_lines.append(line)
                # Also keep any indented attribute lines that belong to it
                j = i + 1
                while j < n and (lines[j].startswith("    ") or lines[j].startswith("\t")):
                    out_lines.append(lines[j])
                    j += 1
                i = j
                continue
        # Indented attribute line not matched above — pass through
        out_lines.append(line)
        i += 1

    out_path = REPO_ROOT / "app/i18n/catalogs/zh-Hans-SG/main.ftl"
    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d lines)", out_path, len(out_lines))
    logger.info(
        "Translated: %d  Skipped (English-only): %d",
        len(used_keys), len(skipped_keys),
    )
    if skipped_keys:
        logger.info("First skipped keys: %s", ", ".join(skipped_keys[:5]))


if __name__ == "__main__":
    main()
