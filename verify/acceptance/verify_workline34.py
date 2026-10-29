#!/usr/bin/env python3
"""Work line 4 acceptance harness: run the offline gates and record real results.

This script intentionally does NOT invent results. It runs the commands, reads pytest's
own JSON report, and marks each Gate case as 通过 / 失败 / 未运行 based on what actually
ran. Gates whose cases cannot be automated offline (Gate 5-8) stay 未运行 here and must
be filled by the responsible owner in ``验收结果表.md``.

Scope of this script: work lines 3+4. It never calls Baidu or a model and it never
writes outside its own report directory. The checkup code under test belongs to work
lines 1+2 and is only read.

Usage (from the repository root):
    python verify/acceptance/verify_workline34.py
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "verify" / "acceptance"

# Offline only: make sure a stray shell profile cannot turn on paid network checks.
OFFLINE_ENV_KEYS = ("RUN_BAIDU_SMOKE", "RUN_DEEPSEEK_SMOKE", "BAIDU_MAP_AK", "DEEPSEEK_API_KEY")

# Gate case -> pytest node ids that verify it. Every id must exist and pass, otherwise
# the case is reported as failed (or 未运行 when the file itself is missing).
GATE_CASES = {
    "Gate 1 合成 CheckupReport 契约": {
        "R1 完成报告结构与身份": [
            "tests.test_report_questions::test_model_sees_every_report_group_and_category_key",
        ],
        "R2 不完整证据与未知": [
            "tests.test_report_questions::test_report_facts_exposes_the_null_count_not_a_fake_zero",
            "tests.test_report_questions::test_report_facts_unknown_area_is_not_described_as_blind",
            "tests.test_report_questions::test_report_facts_keeps_point_reachability_separate_from_coverage",
        ],
        "R3 圈内外区别": [
            "tests.test_report_questions::test_point_and_route_level_evidence_never_reach_the_model",
        ],
        "R4 可达语义": [
            "tests.test_report_questions::test_report_facts_keeps_point_reachability_separate_from_coverage",
        ],
    },
    "Gate 2 当前报告问答服务": {
        "Q1 未完成任务提问": [
            "tests.test_report_question_api::test_question_is_refused_before_the_report_exists",
            "tests.test_report_question_api::test_question_on_a_failed_report_is_refused",
            "tests.test_report_question_api::test_question_on_an_unknown_or_expired_report_is_404",
        ],
        "Q2 无模型密钥走事实查询": [
            "tests.test_report_questions::test_report_facts_matches_category_by_name_and_by_id",
            "tests.test_report_question_api::test_question_on_completed_report_returns_the_contract_answer",
        ],
        "Q3 fake模型成功": [
            "tests.test_report_questions::test_valid_model_answer_is_returned_as_the_contract_object",
        ],
        "Q4 无效证据引用被拒": [
            "tests.test_report_questions::test_unusable_model_answers_are_refused_not_shown",
            "tests.test_report_question_api::test_model_answer_with_unknown_evidence_is_502_and_report_is_untouched",
            "tests.test_report_question_api::test_model_answer_without_evidence_is_502",
        ],
        "Q5 模型超时/异常/错误结构": [
            "tests.test_report_question_api::test_model_failure_does_not_change_the_report_or_block_later_questions",
            "tests.test_report_questions::test_invalid_reference_error_names_no_credential_or_provider_detail",
        ],
        "Q6 事实不支持的问题": [
            "tests.test_report_questions::test_report_facts_does_not_invent_an_answer_for_an_unknown_category",
        ],
        "Q7 0与null": [
            "tests.test_report_questions::test_report_facts_reports_a_confirmed_zero_as_zero",
            "tests.test_report_questions::test_report_facts_exposes_the_null_count_not_a_fake_zero",
        ],
        "Q8 请求字段边界（前端超时未验）": [
            "tests.test_report_question_api::test_unagreed_request_fields_are_rejected_not_ignored",
        ],
    },
    "Gate 3 具体POI与路线证据边界": {
        "P1 具体设施名不可得": [
            "tests.test_report_questions::test_point_and_route_level_evidence_never_reach_the_model",
            "tests.test_report_question_api::test_model_answer_cannot_smuggle_point_level_evidence",
        ],
        "P2 不把两个聚合最小值配成一条路线": [
            "tests.test_report_questions::test_point_and_route_level_evidence_never_reach_the_model",
        ],
        "P3 失败路线不生成距离时间": [
            "tests.test_checkup_service::test_failed_center_route_never_invents_distance_or_duration",
        ],
        "P4 圈外候选不冒充圈内命中": [
            "tests.test_report_questions::test_point_and_route_level_evidence_never_reach_the_model",
        ],
        "P5 跨类别同一POI只测一条中心路线": [
            "tests.test_checkup_service::test_same_poi_can_belong_to_two_categories_but_has_one_center_route",
        ],
        "P6 同ID坐标冲突停止报告": [
            "tests.test_checkup_service::test_same_poi_id_with_conflicting_coordinates_stops_the_report",
        ],
    },
    "Gate 4 模型失败不重跑地图": {
        "G4.1-G4.3 失败后原报告仍可GET且未被替换": [
            "tests.test_report_question_api::test_model_answer_with_unknown_evidence_is_502_and_report_is_untouched",
            "tests.test_report_question_api::test_model_failure_does_not_change_the_report_or_block_later_questions",
            "tests.test_report_question_api::test_report_id_and_question_id_stay_bound_to_the_same_checkup",
        ],
        "G4.4 报告对象逐一字段不变": [
            "tests.test_report_question_api::test_model_answer_with_unknown_evidence_is_502_and_report_is_untouched",
        ],
        "G4.6 无模型时走显式report_facts": [
            "tests.test_report_question_api::test_question_on_completed_report_returns_the_contract_answer",
        ],
        "G4.7 409/404/403语义分别可辨": [
            "tests.test_report_question_api::test_question_is_refused_before_the_report_exists",
            "tests.test_report_question_api::test_question_on_an_unknown_or_expired_report_is_404",
            "tests.test_report_question_api::test_question_on_an_untrusted_origin_is_refused",
        ],
    },
    "Gate 5 问答UI接入": {
        "G5.1 生命周期：仅在完成且report_id匹配时可提问": [
            "tests.test_report_question_api::test_question_is_refused_before_the_report_exists",
        ],
        "G5.2 请求：只调用当前报告的 questions，不新建体检": [
            "frontend.tests.reportConversation::the conversation never submits a checkup of its own",
        ],
        "G5.3 状态：loading/成功/失败/事实查询与模型模式可辨": [
            "frontend.tests.reportConversation::the view exposes loading, failure, retry and mode states accessibly",
            "frontend.tests.reportConversation::a successful question lands in the transcript with its evidence and mode",
        ],
        "G5.4 失败恢复：502/409/429/404 不移除原报告，重试只重发问题": [
            "frontend.tests.reportConversation::a 502 answers as retryable and offers to resend the same question",
            "frontend.tests.reportConversation::retry resends exactly the previous question on the same report",
            "frontend.tests.reportConversation::a non-retryable failure offers no retry",
            "tests.test_report_question_api::test_model_failure_does_not_change_the_report_or_block_later_questions",
        ],
        "G5.5 证据跳转：只对 report/group/facility 生效，未批准的 POI 键不伪装": [
            "frontend.tests.reportConversation::only the three current evidence families are resolvable",
            "frontend.tests.reportConversation::the view asks the host to locate evidence instead of manipulating it",
        ],
        "G5.6 可访问性与长度约束": [
            "frontend.tests.reportConversation::the template renders the transcript with its evidence chips",
            "frontend.tests.reportConversation::the view is a separate component with scoped styles only",
            "frontend.tests.reportConversation::the view resets when the bound report changes",
        ],
        "G5.7 切换报告后丢弃旧响应": [
            "frontend.tests.reportConversation::a late answer for a report the user left is discarded",
            "frontend.tests.reportConversation::a stale in-flight request cannot append after reset",
            "frontend.tests.reportConversation::reset clears transcript, failure and retry state on report switch",
            "frontend.tests.reportConversation::switching the backend also discards the previous report state",
        ],
        "G5.8 页面协调：组件经 emit 与宿主解耦": [
            "frontend.tests.reportConversation::the view asks the host to locate evidence instead of manipulating it",
        ],
    },
}

# Cases that cannot be closed offline in this round. They are printed, never silently
# counted as passing.
NOT_AUTOMATED = [
    "Gate 2 Q8 真实慢模型端到端仍未运行；前端已单独给 questions 至少60秒并有离线测试",
    "Gate 5 宿主接线未落地：ReportConversation.vue 与 reportQuestions.ts 已实现并有离线用例，但 CheckupApp.vue 的接线以补丁待甲方审核（宿主文件归甲方单写）",
    "Gate 4 G4.5 端到端验证需要宿主接线后再测；组件侧“只调用 questions、不新建体检”已有离线用例",
    "Gate 6 多轮：待甲方批准共享 Schema 扩展后才有验收对象",
    "Gate 7 空间报告/地图一致性 S1-S10：由甲方执行，乙方只组织记录",
    "Gate 8 真实闭环与展示签收：需要地图/模型额度与浏览器验收",
]


def _env():
    env = dict(os.environ)
    for key in OFFLINE_ENV_KEYS:
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(args, timeout=900, cwd=None):
    result = subprocess.run(
        args, cwd=cwd or ROOT, env=_env(), capture_output=True, text=True, timeout=timeout
    )
    result.stdout = result.stdout.replace(str(ROOT), "<repo>")
    result.stderr = result.stderr.replace(str(ROOT), "<repo>")
    return result


def _git(*args):
    result = _run(["git", *args])
    return result.stdout.strip() if result.returncode == 0 else f"<git {' '.join(args)} failed>"


def run_pytest(targets):
    """Run pytest with a machine-readable report; return (outcomes, summary).

    Uses pytest's built-in JUnit XML so the harness needs no extra plugin.
    """
    xml_path = OUT_DIR / "_pytest_report.xml"
    command = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        f"--junit-xml={xml_path}", *targets,
    ]
    result = _run(command)
    outcomes = {}
    if xml_path.exists():
        import xml.etree.ElementTree as ElementTree

        try:
            tree = ElementTree.parse(xml_path)
        except ElementTree.ParseError as exc:  # pragma: no cover - corrupt report
            print(f"警告：无法解析 pytest XML 报告：{exc}", file=sys.stderr)
        else:
            for case in tree.iter("testcase"):
                path = case.get("file") or ""
                classname = case.get("classname") or ""
                name = case.get("name") or ""
                module = path.replace("/", ".").removesuffix(".py") if path else classname
                node_id = f"{module}::{name}" if module else name
                if case.find("failure") is not None or case.find("error") is not None:
                    outcomes[node_id] = "failed"
                elif case.find("skipped") is not None:
                    outcomes[node_id] = "skipped"
                else:
                    outcomes[node_id] = "passed"
        xml_path.unlink()
    return outcomes, result


def read_junit_cases(xml_path):
    """Read a JUnit XML report into {name: outcome}; name is bare (no module prefix)."""
    import xml.etree.ElementTree as ElementTree

    if not xml_path.exists():
        return {}
    try:
        tree = ElementTree.parse(xml_path)
    except ElementTree.ParseError as exc:  # pragma: no cover - corrupt report
        print(f"警告：无法解析 JUnit 报告 {xml_path.name}：{exc}", file=sys.stderr)
        return {}
    outcomes = {}
    for case in tree.iter("testcase"):
        name = case.get("name") or ""
        if not name:
            continue
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[name] = "failed"
        elif case.find("skipped") is not None:
            outcomes[name] = "skipped"
        else:
            outcomes[name] = "passed"
    xml_path.unlink()
    return outcomes


def frontend_probe():
    """Run the frontend checks with whichever package runner this machine actually has.

    The probed commands are fixed; only the runner is discovered, and the runner that was
    used is recorded so the result can be reproduced. Playwright is never launched: these
    are the offline unit tests, the type checker and the production build only.
    """
    frontend = ROOT / "frontend"
    if not (frontend / "package.json").exists():
        return {"runner": None, "tests": {"state": "未运行", "detail": "frontend/package.json 不存在"}}
    if not (frontend / "node_modules").exists():
        return {"runner": None, "tests": {"state": "未运行", "detail": "frontend/node_modules 未安装"}}

    if shutil.which("pnpm"):
        runner = ["pnpm"]
    elif shutil.which("corepack"):
        corepack_home = os.environ.get("COREPACK_HOME", "").strip()
        if not corepack_home:
            return {
                "runner": None,
                "tests": {"state": "未运行", "detail": "未找到 pnpm；如用 corepack 请设置 COREPACK_HOME 到可写目录"},
            }
        runner = ["corepack", "pnpm"]
    else:
        return {"runner": None, "tests": {"state": "未运行", "detail": "未找到 pnpm 或 corepack"}}

    def last_line(result):
        text = (result.stdout + result.stderr).strip()
        return text.splitlines()[-1] if text else ""

    def summary(result, keyword):
        for line in reversed((result.stdout + result.stderr).splitlines()):
            if keyword in line:
                return line.strip()
        return last_line(result)

    checks = {}
    case_outcomes = {}
    for name, args, keyword in (
        ("tests", ["test"], "pass"),
        ("typecheck", ["exec", "vue-tsc", "-b"], ""),
        ("build", ["build"], "built in"),
    ):
        if name == "tests":
            # Also produce a machine-readable case list so the Gate 5 rows can cite the real
            # node:test results instead of a hand-written "passed".
            junit = OUT_DIR / "_frontend_junit.xml"
            result = _run(
                [*runner, "exec", "node", "--test", f"--test-reporter=junit", f"--test-reporter-destination={junit}",
                 "tests/reportConversation.test.mjs"],
                timeout=900, cwd=frontend,
            )
            case_outcomes = read_junit_cases(junit)
            result = _run([*runner, *args], timeout=900, cwd=frontend)
            checks[name] = {
                "state": "通过" if result.returncode == 0 else "失败",
                "command": " ".join([*runner, *args]),
                "returncode": result.returncode,
                "detail": summary(result, keyword) or f"exit {result.returncode}",
            }
            continue
        result = _run([*runner, *args], timeout=900, cwd=frontend)
        detail = summary(result, keyword) if keyword else last_line(result)
        checks[name] = {
            "state": "通过" if result.returncode == 0 else "失败",
            "command": " ".join([*runner, *args]),
            "returncode": result.returncode,
            "detail": detail or f"exit {result.returncode}",
        }
    return {"runner": runner[0], "cases": case_outcomes, **checks}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Owned by work lines 3+4. `tests.test_checkup_service` verifies the actual POI/route
    # identity invariant, which is what Gate 3 P3/P5/P6 now cite.
    question_modules = [
        "tests.test_report_questions",
        "tests.test_report_question_api",
        "tests.test_checkup_api",
        "tests.test_checkup_service",
    ]
    question_paths = [module.replace(".", "/") + ".py" for module in question_modules]
    missing_files = [p for p in question_paths if not (ROOT / p).exists()]

    outcomes, targeted_run = run_pytest(question_paths)
    full_run = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    frontend = frontend_probe()
    frontend_cases = frontend.pop("cases", {})
    # Python node ids come from pytest, frontend ones from node:test.
    outcomes.update({f"frontend.tests.reportConversation::{name}": state
                     for name, state in frontend_cases.items()})

    gates = {}
    for gate, cases in GATE_CASES.items():
        gate_cases = {}
        for case, node_ids in cases.items():
            if any(node.split("::")[0].replace(".", "/") + ".py" in missing_files for node in node_ids):
                gate_cases[case] = {"state": "未运行", "detail": "测试文件缺失"}
                continue
            # A parametrized test appears once per parameter, with the parameter suffix in
            # brackets. A case passes only when every collected variant passed.
            variants = {node: [n for n in outcomes if n == node or n.startswith(node + "[")] for node in node_ids}
            missing = [node for node, found in variants.items() if not found]
            failures = [
                f"{n}={outcomes[n]}"
                for node, found in variants.items()
                for n in found
                if outcomes[n] != "passed"
            ]
            total = sum(len(found) for found in variants.values())
            if missing:
                gate_cases[case] = {"state": "未运行", "detail": "用例未收集: " + ", ".join(missing)}
            elif failures:
                gate_cases[case] = {"state": "失败", "detail": ", ".join(failures)}
            else:
                gate_cases[case] = {"state": "通过", "detail": f"{total} 个用例通过"}
        gates[gate] = gate_cases

    record = {
        "captured_at": started,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "sha": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "python": sys.version.split()[0],
        "pytest_targeted": {
            "command": "python -m pytest -q -p no:cacheprovider " + " ".join(question_paths),
            "returncode": targeted_run.returncode,
            "tail": (targeted_run.stdout + targeted_run.stderr).strip().splitlines()[-1:] or [""],
        },
        "pytest_full": {
            "command": "python -m pytest -q -p no:cacheprovider",
            "returncode": full_run.returncode,
            "tail": (full_run.stdout + full_run.stderr).strip().splitlines()[-1:] or [""],
        },
        "frontend": frontend,
        "gates": gates,
        "not_automated": NOT_AUTOMATED,
        "offline_env_stripped": list(OFFLINE_ENV_KEYS),
    }

    (OUT_DIR / "验收结果.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    (OUT_DIR / "验收结果.md").write_text(_markdown(record))

    print(_markdown(record))
    all_targeted_passed = targeted_run.returncode == 0 and not missing_files
    all_full_passed = full_run.returncode == 0
    frontend_passed = bool(frontend.get("runner")) and all(
        frontend[name]["state"] == "通过" for name in ("tests", "typecheck", "build")
    )
    return 0 if all_targeted_passed and all_full_passed and frontend_passed else 1


def _markdown(record):
    lines = []
    lines.append("# 工作线3+4离线验收记录（脚本生成，请勿手工改写结论）")
    lines.append("")
    lines.append(f"- 采集时间：{record['captured_at']}")
    lines.append(f"- 分支：`{record['branch']}`")
    lines.append(f"- 完整SHA：`{record['sha']}`")
    lines.append(f"- 工作区有未提交改动：{'是' if record['working_tree_dirty'] else '否'}")
    lines.append(f"- Python：{record['python']}")
    lines.append(f"- 联网调用次数：未统计；脚本已剔除 {', '.join(record['offline_env_stripped'])}，本记录不作为真实联网验收")
    lines.append("")
    lines.append("## 命令与结果")
    lines.append("")
    lines.append("| 命令 | 退出码 | 末行 |")
    lines.append("| --- | --- | --- |")
    for key in ("pytest_targeted", "pytest_full"):
        entry = record[key]
        lines.append(f"| `{entry['command']}` | {entry['returncode']} | {entry['tail'][0]} |")
    frontend = record["frontend"]
    if frontend.get("runner"):
        for name, label in (("tests", "前端测试"), ("typecheck", "前端类型检查"), ("build", "前端生产构建")):
            entry = frontend[name]
            lines.append(f"| `{entry['command']}`（{label}） | {entry['returncode']} | {entry['detail']} |")
    else:
        lines.append(f"| `pnpm test` / `vue-tsc -b` / `pnpm build` | - | {frontend['tests']['state']}：{frontend['tests']['detail']} |")
    lines.append("")
    lines.append("## Gate 结果（仅填 通过/失败/未运行）")
    lines.append("")
    lines.append("| Gate / 用例 | 结果 | 依据 |")
    lines.append("| --- | --- | --- |")
    for gate, cases in record["gates"].items():
        for case, entry in cases.items():
            lines.append(f"| {gate} / {case} | {entry['state']} | {entry['detail']} |")
    lines.append("")
    lines.append("## 本轮未自动化、需对应负责人填写的门")
    lines.append("")
    for item in record["not_automated"]:
        lines.append(f"- 未运行：{item}")
    lines.append("")
    lines.append("> 离线通过、合成接合、真实单地点、完整展示验收是四个不同结论，不可互相替代。")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
