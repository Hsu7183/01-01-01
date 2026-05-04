from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .modular_0313plus import (
    describe_0313plus_template_choices,
    is_modular_0313plus_template_choices,
    render_modular_0313plus_xs,
)
from .paths import generated_strategy_dir
from .types import BacktestMetrics, CandidateProposal, StrategyArtifact
from .xscript_policy import get_policy_reference_text


DISPLAY_PARAM_ORDER = [
    "DonLen",
    "ATRLen",
    "EMAWarmBars",
    "EntryBufferPts",
    "DonBufferPts",
    "MinATRD",
    "ATRStopK",
    "ATRTakeProfitK",
    "MaxEntriesPerDay",
    "TimeStopBars",
    "MinRunPctAnchor",
    "TrailStartPctAnchor",
    "TrailGivePctAnchor",
    "UseAnchorExit",
    "AnchorBackPct",
]


def build_strategy_signature(proposal: CandidateProposal) -> str:
    payload = {
        "params": dict(sorted(proposal.params.items())),
        "template_choices": dict(sorted(proposal.template_choices.items())),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def render_strategy_xs(base_xs_text: str, proposal: CandidateProposal, out_dir: str) -> str:
    path = Path(out_dir) / "strategy.xs"
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered_text = base_xs_text
    if is_modular_0313plus_template_choices(proposal.template_choices):
        rendered_text = render_modular_0313plus_xs(base_xs_text, proposal.template_choices)
    path.write_text(rendered_text, encoding="utf-8")
    return str(path)


def _ordered_param_items(proposal: CandidateProposal) -> list[tuple[str, object]]:
    ordered_names = DISPLAY_PARAM_ORDER + [name for name in proposal.params if name not in DISPLAY_PARAM_ORDER]
    return [(name, proposal.params[name]) for name in ordered_names if name in proposal.params]


def _ordered_template_items(proposal: CandidateProposal) -> list[tuple[str, object]]:
    ordered_names = [
        "strategy_group_label",
        "bias_mode",
        "entry_mode",
        "atr_filter_mode",
        "use_atr_stop",
        "use_atr_tp",
        "use_time_stop",
        "use_trail_exit",
    ]
    return [(name, proposal.template_choices[name]) for name in ordered_names if name in proposal.template_choices]


def render_params_txt(
    proposal: CandidateProposal,
    out_dir: str,
    metrics: BacktestMetrics | None = None,
) -> str:
    path = Path(out_dir) / "params.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    header_parts = [f"{name}={value}" for name, value in _ordered_param_items(proposal)]
    if is_modular_0313plus_template_choices(proposal.template_choices):
        concept = describe_0313plus_template_choices(proposal.template_choices)
        header_parts = [f"{name}={value}" for name, value in _ordered_template_items(proposal)] + header_parts + [f"Strategy={concept}"]
    if not header_parts:
        header_parts = ["params=none"]

    lines = [",".join(header_parts)]
    trade_lines = list((metrics.trade_lines if metrics is not None else []) or [])
    if trade_lines:
        lines.extend(trade_lines)
    else:
        lines.append("無逐筆交易資料")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def write_policy_reference(out_dir: str) -> str:
    path = Path(out_dir) / "xscript_policy_reference.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(get_policy_reference_text(), encoding="utf-8")
    return str(path)


def write_strategy_artifacts(
    base_xs_path: str,
    proposal: CandidateProposal,
    metrics: BacktestMetrics | None = None,
) -> StrategyArtifact:
    base_xs_text = Path(base_xs_path).read_text(encoding="utf-8")
    signature = build_strategy_signature(proposal)
    strategy_id = f"s_{signature[:12]}"
    out_dir = generated_strategy_dir() / strategy_id
    xs_path = render_strategy_xs(base_xs_text, proposal, str(out_dir))
    params_txt_path = render_params_txt(proposal, str(out_dir), metrics=metrics)
    write_policy_reference(str(out_dir))
    return StrategyArtifact(
        strategy_id=strategy_id,
        signature=signature,
        out_dir=str(out_dir),
        xs_path=xs_path,
        params_txt_path=params_txt_path,
    )
