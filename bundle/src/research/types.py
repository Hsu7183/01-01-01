from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ResearchConfig:
    session_id: str
    model: str
    base_xs_path: str
    minute_path: str
    daily_path: str
    seed_source: str = "current_xs_defaults"
    seed_label: str | None = None
    seed_params: dict[str, Any] = field(default_factory=dict)
    exploration_mode: str = "seed_local"
    param_preset_path: str | None = None
    txt_path: str | None = None
    batch_size: int = 20
    allow_param_mutation: bool = True
    allow_template_mutation: bool = False
    top_n: int = 10
    capital: int = 1_000_000
    slip_per_side: float = 2.0
    min_trades: int = 300
    min_total_return: float = 5.0
    max_mdd_pct: float = 40.0
    runtime_script_name: str = "0313plus"
    max_rounds: int | None = None


@dataclass(slots=True)
class CandidateProposal:
    params: dict[str, Any]
    ai_summary: str = ""
    parent_strategy_id: str | None = None
    template_choices: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BacktestMetrics:
    total_return: float
    mdd_pct: float
    n_trades: int
    year_avg_return: float
    year_return_std: float
    loss_years: int
    composite_score: float
    fail_reason: str | None = None
    passed_hard_filters: bool = True
    trade_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StrategyArtifact:
    strategy_id: str
    signature: str
    out_dir: str
    xs_path: str
    params_txt_path: str


@dataclass(slots=True)
class ResearchStatus:
    session_id: str
    status: str
    current_round: int = 0
    tested_count: int = 0
    best_strategy_id: str | None = None
    best_score: float | None = None
    current_action: str | None = None
    current_candidate_label: str | None = None
    last_completed_strategy_id: str | None = None
    current_strategy_group_code: str | None = None
    current_strategy_group_label: str | None = None
    current_strategy_group_summary: str | None = None
    current_strategy_group_order_text: str | None = None
    current_param_scope_text: str | None = None
    current_candidate_params_text: str | None = None
    current_candidate_index: int = 0
    current_candidate_total: int = 0
    strategy_groups_completed: int = 0
    params_tested_in_group: int = 0
    params_total_in_group: int = 0
    current_phase: str | None = None
    session_elapsed_seconds: int = 0
    compute_elapsed_seconds: int = 0
    wait_elapsed_seconds: int = 0
    last_error: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
