import os
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Literal

import psycopg
from dotenv import load_dotenv
from allocation_rules import compute_allocation_by_stup, get_network_relation_rows
from collateral_review_rules import get_final_review_amounts_by_stup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


load_dotenv(Path(__file__).with_name(".env"))

DB_CONFIG = {
    "host": os.getenv("PGHOST", "127.0.0.1"),
    "port": int(os.getenv("PGPORT", "5432")),
    "dbname": os.getenv("PGDATABASE", "collateral_agent"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}

TABLE_SQL: Dict[str, str] = {
    "TB_CBK_LNM009M": """
        SELECT
            "MTN",
            "MRTG_KCD",
            "ETC_SBTC_AMT",
            "MRAK_RTO"
        FROM public."TB_CBK_LNM009M"
        {where_clause}
        ORDER BY "MTN"
    """,
    "TB_CBK_LNM010L": """
        SELECT
            "MTN",
            "SBSD_OBJ_SRN",
            "MRTG_SCD",
            "RGSN_YMD",
            "APRS_PRC",
            "VLTG_EX_AMT",
            "RSSM_APRS_AMT"
        FROM public."TB_CBK_LNM010L"
        {where_clause}
        ORDER BY "MTN", "SBSD_OBJ_SRN"
    """,
    "TB_CBK_LNM051M": """
        SELECT
            "STUP_NO",
            "STUP_SCD",
            "TRMT_YMD",
            "MNGM_BRCD",
            "STUP_YMD",
            "BND_MAX_AMT",
            "STRTO_DCD",
            "DBTR_EDPS_CSN",
            "BNDR_DCD"
        FROM public."TB_CBK_LNM051M"
        {where_clause}
        ORDER BY "STUP_NO"
    """,
    "TB_CBK_LNM052R": """
        SELECT
            "STUP_NO",
            "MTN",
            "SBSD_OBJ_SRN",
            "STMR_RLTN_SCD",
            "STUP_ORD",
            "MRTG_CLSF_DCD"
        FROM public."TB_CBK_LNM052R"
        {where_clause}
        ORDER BY "STUP_NO", "MTN", "SBSD_OBJ_SRN"
    """,
    "TB_CBK_LNM053R": """
        SELECT
            "STUP_NO",
            "ICN",
            "MRTG_ARR_SRN",
            "PMBN_SCD",
            "RGSN_YMD"
        FROM public."TB_CBK_LNM053R"
        {where_clause}
        ORDER BY "STUP_NO", "RGSN_YMD", "ICN"
    """,
    "TB_CBK_ARG001M": """
        SELECT
            "ICN",
            "ARR_SCD",
            "EDPS_CSN",
            "ARR_AMT"
        FROM public."TB_CBK_ARG001M"
        {where_clause}
        ORDER BY "ICN"
    """,
}

MRTG_KCD_NAMES = {
    "101": "Apartment",
    "115": "Factory",
    "121": "Neighborhood Facility",
}


app = FastAPI(
    title="Collateral Allocation Agent Prototype API",
    description="Prototype API for collateral, setup, and allocation lookups",
    version="0.5.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    identifier_type: Literal["account", "collateral"]
    identifier_value: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=3, max_length=1000)


class AnalysisResult(BaseModel):
    intent_code: str
    intent_label: str
    analysis_focus: List[str]
    response_format: str
    inferred_tables: List[str]


SHORTFALL_KEYWORDS = [
    "why",
    "reason",
    "shortfall",
    "credit",
    "full collateral",
    "jeonaek",
    "순신용",
    "전액담보",
]
SENIORITY_KEYWORDS = ["senior", "priority", "first", "before", "선순위", "우선", "먼저"]
SHARED_COLLATERAL_KEYWORDS = ["shared collateral", "joint collateral", "pool", "공담", "공동담보"]
SETTING_KEYWORDS = ["setting", "mortgage", "lien", "limit", "설정", "근저당", "한도"]


def contains_any(text: str, keywords: List[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def analyze_question(question: str, identifier_type: str) -> AnalysisResult:
    normalized = question.strip().lower()
    inferred_tables = [
        "TB_CBK_LNM009M",
        "TB_CBK_LNM010L",
        "TB_CBK_LNM051M",
        "TB_CBK_LNM052R",
        "TB_CBK_LNM053R",
        "TB_CBK_ARG001M",
    ]

    if contains_any(normalized, SHORTFALL_KEYWORDS) or contains_any(question, SHORTFALL_KEYWORDS):
        return AnalysisResult(
            intent_code="analyze_shortfall_reason",
            intent_label="Shortfall reason analysis",
            analysis_focus=[
                "senior claim allocation",
                "setting allocation limit",
                "collateral final review amount",
            ],
            response_format="tree_with_reason_breakdown",
            inferred_tables=inferred_tables,
        )

    if contains_any(normalized, SENIORITY_KEYWORDS) or contains_any(question, SENIORITY_KEYWORDS):
        return AnalysisResult(
            intent_code="show_senior_claims",
            intent_label="Senior claim allocation lookup",
            analysis_focus=["senior claims", "allocation order", "allocated amounts"],
            response_format="tree_with_amounts",
            inferred_tables=inferred_tables,
        )

    if contains_any(normalized, SHARED_COLLATERAL_KEYWORDS) or contains_any(question, SHARED_COLLATERAL_KEYWORDS):
        return AnalysisResult(
            intent_code="analyze_shared_collateral",
            intent_label="Shared collateral analysis",
            analysis_focus=["shared collateral links", "settings per collateral", "cross-impact"],
            response_format="tree_with_links",
            inferred_tables=inferred_tables,
        )

    if contains_any(normalized, SETTING_KEYWORDS) or contains_any(question, SETTING_KEYWORDS):
        return AnalysisResult(
            intent_code="inspect_setting_capacity",
            intent_label="Setting capacity inspection",
            analysis_focus=["bond max amount", "setting available amount", "distributable amount"],
            response_format="capacity_breakdown_tree",
            inferred_tables=inferred_tables,
        )

    entity_focus = "account-linked structure" if identifier_type == "account" else "collateral-linked structure"
    return AnalysisResult(
        intent_code="general_structure_lookup",
        intent_label="General structure lookup",
        analysis_focus=[entity_focus, "setting links", "secured claim links"],
        response_format="structure_tree",
        inferred_tables=inferred_tables,
    )


def build_lookup_plan(request: AnalyzeRequest, analysis: AnalysisResult) -> List[str]:
    if request.identifier_type == "collateral":
        return [
            f"Resolve settings connected to collateral {request.identifier_value}.",
            "Expand into collateral master, collateral objects, and secured claim relations.",
            "Calculate final review amount per collateral and distributable amount per setting.",
            f"Summarize results for intent: {analysis.intent_label}.",
        ]

    return [
        f"Resolve settings connected to account {request.identifier_value}.",
        "Expand into linked collateral, collateral objects, and other secured claims on the same setting.",
        "Calculate allocation order and find senior claims allocated before the target account.",
        f"Summarize results for intent: {analysis.intent_label}.",
    ]


def run_query(sql: str, params: tuple = ()) -> List[dict]:
    with psycopg.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [desc.name for desc in cur.description]

    normalized_rows = []
    for row in rows:
        item = {}
        for key, value in zip(columns, row):
            if isinstance(value, Decimal):
                item[key] = float(value)
            else:
                item[key] = value
        normalized_rows.append(item)
    return normalized_rows


def fetch_table(table_name: str, filter_value: str | None = None) -> List[dict]:
    if table_name not in TABLE_SQL:
        raise HTTPException(status_code=404, detail=f"Unsupported table: {table_name}")

    where_clause = ""
    params: tuple = ()

    if filter_value:
        numeric_value = int(filter_value)
        if table_name in {"TB_CBK_LNM009M", "TB_CBK_LNM010L"}:
            where_clause = 'WHERE "MTN" = %s'
        elif table_name == "TB_CBK_ARG001M":
            where_clause = 'WHERE "ICN" = %s'
        else:
            where_clause = 'WHERE "STUP_NO" = %s'
        params = (numeric_value,)

    sql = TABLE_SQL[table_name].format(where_clause=where_clause)
    return run_query(sql, params)


def get_setting_numbers(identifier_type: str, identifier_value: str) -> List[int]:
    if not identifier_value.isdigit():
        return []

    if identifier_type == "account":
        rows = run_query(
            """
            SELECT DISTINCT "STUP_NO"
            FROM public."TB_CBK_LNM053R"
            WHERE "ICN" = %s
            ORDER BY "STUP_NO"
            """,
            (int(identifier_value),),
        )
    else:
        rows = run_query(
            """
            SELECT DISTINCT "STUP_NO"
            FROM public."TB_CBK_LNM052R"
            WHERE "MTN" = %s
            ORDER BY "STUP_NO"
            """,
            (int(identifier_value),),
        )

    return [int(row["STUP_NO"]) for row in rows]


def get_mrtg_kcd_name(code: str | None) -> str:
    return MRTG_KCD_NAMES.get(code or "", "Other")


def get_senior_setting_numbers_for_account(target_icn: int, base_setting_numbers: List[int]) -> set[int]:
    senior_setting_numbers: set[int] = set()

    for base_stup_no in base_setting_numbers:
        relation_rows = get_network_relation_rows(DB_CONFIG, base_stup_no)
        target_relations = [row for row in relation_rows if row["stup_no"] == base_stup_no]

        for target_relation in target_relations:
            target_priority = (
                target_relation["stup_ymd"],
                target_relation["stup_ord"],
                target_relation["stup_no"],
            )

            for relation in relation_rows:
                if relation["mtn"] != target_relation["mtn"]:
                    continue
                relation_priority = (
                    relation["stup_ymd"],
                    relation["stup_ord"],
                    relation["stup_no"],
                )
                if relation_priority < target_priority:
                    senior_setting_numbers.add(relation["stup_no"])

    return senior_setting_numbers


def build_single_setting_tree(
    stup_no: int,
    identifier_type: str,
    target_icn: int | None,
    target_mtn: int | None,
    senior_icns: set[int],
    senior_setting_numbers: set[int],
) -> dict | None:
    setting_rows = fetch_table("TB_CBK_LNM051M", str(stup_no))
    if not setting_rows:
        return None

    setting_row = setting_rows[0]
    relation_rows = fetch_table("TB_CBK_LNM052R", str(stup_no))
    claim_relation_rows = fetch_table("TB_CBK_LNM053R", str(stup_no))
    allocation_result = compute_allocation_by_stup(DB_CONFIG, stup_no)

    review_by_mtn = {int(row["mtn"]): row for row in allocation_result["collateral_final_reviews"]}
    relation_by_mtn = {int(row["MTN"]): row for row in relation_rows}

    collateral_nodes: List[dict] = []
    for mtn, review_row in review_by_mtn.items():
        relation_row = relation_by_mtn.get(mtn, {})
        collateral_nodes.append(
            {
                "mtn": mtn,
                "mrtg_kcd": review_row["mrtg_kcd"],
                "mrtg_kcd_name": get_mrtg_kcd_name(review_row["mrtg_kcd"]),
                "stup_ord": relation_row.get("STUP_ORD"),
                "final_review_amount": review_row["final_review_amount"],
                "reserved_for_setting_amount": review_row.get("reserved_for_setting_amount", review_row["final_review_amount"]),
                "remaining_after_network_allocation": review_row.get("remaining_after_network_allocation", 0.0),
                "object_sum_before_deduction": review_row["object_sum_before_deduction"],
                "etc_sbtc_amt": review_row["etc_sbtc_amt"],
                "mrak_rto": review_row["mrak_rto"],
                "claim_allocations": review_row.get("claim_allocations", []),
                "is_target": target_mtn == mtn,
            }
        )

    ordered_claim_relations = sorted(
        claim_relation_rows,
        key=lambda row: (int(row["RGSN_YMD"]), int(row["ICN"])),
    )

    target_claim_index = None
    if target_icn is not None:
        for index, relation in enumerate(ordered_claim_relations):
            if int(relation["ICN"]) == target_icn:
                target_claim_index = index
                break

    claim_nodes: List[dict] = []
    for index, claim_row in enumerate(allocation_result["allocations"]):
        icn = int(claim_row["icn"])
        is_target = target_icn is not None and icn == target_icn
        is_senior_to_target = target_claim_index is not None and index < target_claim_index
        is_known_senior = icn in senior_icns

        if identifier_type == "account":
            if is_target:
                role = "target"
            elif is_senior_to_target or is_known_senior or stup_no in senior_setting_numbers:
                role = "senior"
            else:
                role = "junior"
        else:
            role = "allocation_order"

        claim_nodes.append(
            {
                "order_no": index + 1,
                "icn": icn,
                "rgsn_ymd": int(claim_row["rgsn_ymd"]),
                "arr_amt": float(claim_row["arr_amt"]),
                "allocated_amount": float(claim_row["allocated_amount"]),
                "shortfall_amount": float(claim_row["shortfall_amount"]),
                "sun_credit_amount": float(claim_row["sun_credit_amount"]),
                "collateral_allocations": claim_row.get("collateral_allocations", []),
                "arr_scd": claim_row["arr_scd"],
                "edps_csn": int(claim_row["edps_csn"]),
                "role": role,
                "is_target": is_target,
                "is_senior_to_target": role == "senior",
            }
        )

    return {
        "stup_no": stup_no,
        "setting": {
            "stup_no": int(setting_row["STUP_NO"]),
            "stup_ymd": int(setting_row["STUP_YMD"]),
            "mngm_brcd": setting_row["MNGM_BRCD"],
            "bnd_max_amt": float(setting_row["BND_MAX_AMT"]),
            "setting_available_amount": allocation_result["setting_available_amount"],
            "collateral_final_review_total": allocation_result["collateral_final_review_total"],
            "shared_collateral_usable_amount": allocation_result.get("shared_collateral_usable_amount", allocation_result["distributable_amount"]),
            "distributable_amount": allocation_result["distributable_amount"],
            "remaining_amount": allocation_result["remaining_amount"],
            "priority_rule": allocation_result["priority_rule"],
            "shared_collateral_priority_rule": allocation_result.get("shared_collateral_priority_rule"),
        },
        "collaterals": sorted(
            collateral_nodes,
            key=lambda row: (
                row["stup_ord"] if row["stup_ord"] is not None else 9999,
                row["mtn"],
            ),
        ),
        "claims": claim_nodes,
    }


def build_allocation_tree(identifier_type: str, identifier_value: str) -> dict:
    target_icn = int(identifier_value) if identifier_type == "account" and identifier_value.isdigit() else None
    target_mtn = int(identifier_value) if identifier_type == "collateral" and identifier_value.isdigit() else None
    base_setting_numbers = get_setting_numbers(identifier_type, identifier_value)
    setting_numbers = list(base_setting_numbers)
    senior_icns: set[int] = set()
    senior_setting_numbers: set[int] = set()

    if identifier_type == "account" and target_icn is not None:
        senior_setting_numbers = get_senior_setting_numbers_for_account(target_icn, base_setting_numbers)
        setting_numbers = sorted(set(base_setting_numbers) | senior_setting_numbers)

        for stup_no in base_setting_numbers:
            tree = build_single_setting_tree(stup_no, identifier_type, target_icn, target_mtn, set(), senior_setting_numbers)
            if not tree:
                continue
            for claim in tree["claims"]:
                if claim["role"] == "senior":
                    senior_icns.add(int(claim["icn"]))

        extra_setting_numbers: set[int] = set(setting_numbers)
        for senior_icn in senior_icns:
            for senior_stup_no in get_setting_numbers("account", str(senior_icn)):
                extra_setting_numbers.add(senior_stup_no)

        setting_numbers = sorted(set(base_setting_numbers) | extra_setting_numbers)

    trees: List[dict] = []
    total_senior_allocated = 0.0
    total_target_allocated = 0.0
    total_target_shortfall = 0.0

    for stup_no in setting_numbers:
        tree = build_single_setting_tree(
            stup_no,
            identifier_type,
            target_icn,
            target_mtn,
            senior_icns,
            senior_setting_numbers,
        )
        if not tree:
            continue

        for claim in tree["claims"]:
            if claim["role"] == "senior":
                total_senior_allocated += float(claim["allocated_amount"])
            if claim["role"] == "target":
                total_target_allocated += float(claim["allocated_amount"])
                total_target_shortfall += float(claim["shortfall_amount"])

        trees.append(tree)

    return {
        "identifier_type": identifier_type,
        "identifier_value": identifier_value,
        "setting_count": len(trees),
        "target_summary": {
            "total_senior_allocated": total_senior_allocated,
            "total_target_allocated": total_target_allocated,
            "total_target_shortfall": total_target_shortfall,
        },
        "trees": trees,
    }


@app.get("/")
def read_root() -> dict:
    return {
        "service": "collateral-allocation-agent-prototype",
        "message": "Use /api/health, /api/ledger/*, /api/analyze-query, and /api/allocation-tree.",
    }


@app.get("/api/health")
def health_check() -> dict:
    try:
        result = run_query("SELECT current_database() AS db_name, current_user AS user_name")
        return {
            "status": "ok",
            "database": result[0]["db_name"],
            "user": result[0]["user_name"],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {exc}") from exc


@app.get("/api/ledger/{table_name}")
def get_ledger(
    table_name: str,
    filter_value: str | None = Query(default=None, description="MTN, STUP_NO or ICN filter"),
) -> dict:
    rows = fetch_table(table_name.upper(), filter_value)
    return {
        "table": table_name.upper(),
        "filter_value": filter_value,
        "row_count": len(rows),
        "rows": rows,
    }


@app.get("/api/ledger-summary/stup/{stup_no}")
def get_stup_summary(stup_no: int) -> dict:
    setting_rows = fetch_table("TB_CBK_LNM051M", str(stup_no))
    relation_rows = fetch_table("TB_CBK_LNM052R", str(stup_no))
    claim_rows = fetch_table("TB_CBK_LNM053R", str(stup_no))

    contract_rows: List[dict] = []
    collateral_rows: List[dict] = []
    object_rows: List[dict] = []
    seen_mtn = set()

    for relation in relation_rows:
        mtn = relation["MTN"]
        if mtn not in seen_mtn:
            collateral_rows.extend(fetch_table("TB_CBK_LNM009M", str(mtn)))
            object_rows.extend(fetch_table("TB_CBK_LNM010L", str(mtn)))
            seen_mtn.add(mtn)

    for claim in claim_rows:
        contract_rows.extend(fetch_table("TB_CBK_ARG001M", str(claim["ICN"])))

    return {
        "stup_no": stup_no,
        "setting_master": setting_rows,
        "collateral_relations": relation_rows,
        "secured_claim_relations": claim_rows,
        "contract_master": contract_rows,
        "collateral_master": collateral_rows,
        "collateral_objects": object_rows,
    }


@app.get("/api/final-review/stup/{stup_no}")
def get_final_review_by_stup(stup_no: int) -> dict:
    try:
        review_rows = get_final_review_amounts_by_stup(DB_CONFIG, stup_no)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Final review calculation failed: {exc}") from exc

    return {
        "stup_no": stup_no,
        "rule": "sum((reappraisal if > 0 else appraisal) - outside value) by MTN, subtract ETC_SBTC_AMT, then multiply by MRAK_RTO / 100",
        "row_count": len(review_rows),
        "rows": review_rows,
    }


@app.get("/api/allocation/stup/{stup_no}")
def get_allocation_by_stup(stup_no: int) -> dict:
    try:
        result = compute_allocation_by_stup(DB_CONFIG, stup_no)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Allocation calculation failed: {exc}") from exc

    return {
        "stup_no": stup_no,
        "rule": "distributable amount = min(collateral final review total, bond max amount / 1.2); allocate claims by RGSN_YMD asc then ICN asc; shortfall is exposed as sun credit",
        **result,
    }


@app.post("/api/allocation-tree")
def get_allocation_tree(request: AnalyzeRequest) -> dict:
    if not request.identifier_value.isdigit():
        raise HTTPException(
            status_code=400,
            detail="This prototype currently supports numeric ICN or MTN values for live DB allocation lookup.",
        )

    try:
        analysis = analyze_question(request.question, request.identifier_type)
        tree = build_allocation_tree(request.identifier_type, request.identifier_value)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Allocation tree lookup failed: {exc}") from exc

    return {
        "message": "Live DB lookup completed.",
        "request": {
            "identifier_type": request.identifier_type,
            "identifier_value": request.identifier_value,
            "question": request.question,
        },
        "analysis": analysis.model_dump(),
        "allocation_tree": tree,
    }


@app.post("/api/analyze-query")
def analyze_query(request: AnalyzeRequest) -> dict:
    analysis = analyze_question(request.question, request.identifier_type)
    lookup_plan = build_lookup_plan(request, analysis)
    preview_rows: List[dict] = []

    if request.identifier_type == "collateral" and request.identifier_value.isdigit():
        preview_rows = fetch_table("TB_CBK_LNM009M", request.identifier_value)

    return {
        "message": "Question interpretation completed.",
        "request": {
            "identifier_type": request.identifier_type,
            "identifier_value": request.identifier_value,
            "question": request.question,
        },
        "analysis": analysis.model_dump(),
        "lookup_plan": lookup_plan,
        "collateral_preview": preview_rows,
    }
