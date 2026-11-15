from collections import defaultdict, deque
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List

import psycopg

from collateral_review_rules import get_final_review_amounts_by_stup


TWOPLACES = Decimal("0.01")
SETTING_COVERAGE_RATIO = Decimal("1.2")


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def get_relation_rows(db_config: Dict[str, object]) -> List[dict]:
    sql = """
        SELECT
            r."STUP_NO",
            r."MTN",
            MIN(r."STUP_ORD") AS "STUP_ORD",
            s."STUP_YMD",
            s."BND_MAX_AMT"
        FROM public."TB_CBK_LNM052R" r
        JOIN public."TB_CBK_LNM051M" s
            ON r."STUP_NO" = s."STUP_NO"
        GROUP BY
            r."STUP_NO",
            r."MTN",
            s."STUP_YMD",
            s."BND_MAX_AMT"
        ORDER BY
            s."STUP_YMD",
            MIN(r."STUP_ORD"),
            r."STUP_NO",
            r."MTN"
    """
    with psycopg.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    results: List[dict] = []
    for row in rows:
        results.append(
            {
                "stup_no": int(row[0]),
                "mtn": int(row[1]),
                "stup_ord": int(row[2]),
                "stup_ymd": int(row[3]),
                "bnd_max_amt": to_decimal(row[4]),
            }
        )
    return results


def get_network_relation_rows(db_config: Dict[str, object], target_stup_no: int) -> List[dict]:
    all_relations = get_relation_rows(db_config)
    relations_by_stup: Dict[int, List[dict]] = defaultdict(list)
    relations_by_mtn: Dict[int, List[dict]] = defaultdict(list)

    for relation in all_relations:
        relations_by_stup[relation["stup_no"]].append(relation)
        relations_by_mtn[relation["mtn"]].append(relation)

    visited_stups = {target_stup_no}
    visited_mtns = set()
    queue = deque([("stup", target_stup_no)])

    while queue:
        node_type, node_value = queue.popleft()

        if node_type == "stup":
            for relation in relations_by_stup.get(node_value, []):
                mtn = relation["mtn"]
                if mtn not in visited_mtns:
                    visited_mtns.add(mtn)
                    queue.append(("mtn", mtn))
        else:
            for relation in relations_by_mtn.get(node_value, []):
                stup_no = relation["stup_no"]
                if stup_no not in visited_stups:
                    visited_stups.add(stup_no)
                    queue.append(("stup", stup_no))

    return [
        relation
        for relation in all_relations
        if relation["stup_no"] in visited_stups and relation["mtn"] in visited_mtns
    ]


def get_claim_rows_by_stup(db_config: Dict[str, object], stup_no: int) -> List[dict]:
    sql = """
        SELECT
            r."STUP_NO",
            r."ICN",
            r."MRTG_ARR_SRN",
            r."RGSN_YMD",
            a."ARR_SCD",
            a."EDPS_CSN",
            a."ARR_AMT"
        FROM public."TB_CBK_LNM053R" r
        JOIN public."TB_CBK_ARG001M" a
            ON r."ICN" = a."ICN"
        WHERE r."STUP_NO" = %s
        ORDER BY r."RGSN_YMD", r."ICN"
    """
    with psycopg.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stup_no,))
            rows = cur.fetchall()

    claims: List[dict] = []
    for row in rows:
        claims.append(
            {
                "stup_no": int(row[0]),
                "icn": int(row[1]),
                "mrtg_arr_srn": int(row[2]),
                "rgsn_ymd": int(row[3]),
                "arr_scd": row[4],
                "edps_csn": int(row[5]),
                "arr_amt": to_decimal(row[6]),
            }
        )
    return claims


def get_final_review_by_mtn_for_network(db_config: Dict[str, object], relation_rows: List[dict]) -> Dict[int, dict]:
    unique_stups = sorted({relation["stup_no"] for relation in relation_rows})
    by_mtn: Dict[int, dict] = {}

    for stup_no in unique_stups:
        for row in get_final_review_amounts_by_stup(db_config, stup_no):
            by_mtn.setdefault(int(row["mtn"]), row)

    return by_mtn


def build_setting_summaries(relation_rows: List[dict]) -> Dict[int, dict]:
    summaries: Dict[int, dict] = {}
    for relation in relation_rows:
        stup_no = relation["stup_no"]
        summary = summaries.setdefault(
            stup_no,
            {
                "stup_no": stup_no,
                "stup_ymd": relation["stup_ymd"],
                "bnd_max_amt": relation["bnd_max_amt"],
                "relations": [],
            },
        )
        summary["relations"].append(relation)
    return summaries


def compute_network_allocation(db_config: Dict[str, object], target_stup_no: int) -> dict:
    relation_rows = get_network_relation_rows(db_config, target_stup_no)
    if not relation_rows:
        raise ValueError(f"STUP_NO {target_stup_no} not found")

    final_review_by_mtn = get_final_review_by_mtn_for_network(db_config, relation_rows)
    setting_summaries = build_setting_summaries(relation_rows)

    collateral_remaining: Dict[int, Decimal] = {
        mtn: to_decimal(row["final_review_amount"]) for mtn, row in final_review_by_mtn.items()
    }
    collateral_original = collateral_remaining.copy()

    setting_capacity_remaining: Dict[int, Decimal] = {}
    for stup_no, summary in setting_summaries.items():
        setting_capacity_remaining[stup_no] = quantize_money(summary["bnd_max_amt"] / SETTING_COVERAGE_RATIO)

    reserved_by_setting_mtn: Dict[int, Dict[int, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    reservation_trace: List[dict] = []

    sorted_relations = sorted(
        relation_rows,
        key=lambda row: (row["stup_ymd"], row["stup_ord"], row["stup_no"], row["mtn"]),
    )

    for relation in sorted_relations:
        stup_no = relation["stup_no"]
        mtn = relation["mtn"]
        available_for_setting = setting_capacity_remaining[stup_no]
        available_for_collateral = collateral_remaining.get(mtn, Decimal("0"))
        reserved_amount = quantize_money(min(available_for_setting, available_for_collateral))

        if reserved_amount <= 0:
            continue

        reserved_by_setting_mtn[stup_no][mtn] += reserved_amount
        setting_capacity_remaining[stup_no] = quantize_money(available_for_setting - reserved_amount)
        collateral_remaining[mtn] = quantize_money(available_for_collateral - reserved_amount)

        reservation_trace.append(
            {
                "stup_no": stup_no,
                "mtn": mtn,
                "stup_ymd": relation["stup_ymd"],
                "stup_ord": relation["stup_ord"],
                "reserved_amount": float(reserved_amount),
                "setting_capacity_remaining": float(setting_capacity_remaining[stup_no]),
                "collateral_remaining": float(collateral_remaining[mtn]),
            }
        )

    results_by_stup: Dict[int, dict] = {}

    for stup_no, summary in setting_summaries.items():
        claim_rows = get_claim_rows_by_stup(db_config, stup_no)
        reserved_total = quantize_money(sum(reserved_by_setting_mtn[stup_no].values(), Decimal("0")))

        remaining_amount = reserved_total
        allocations: List[dict] = []
        collateral_claim_allocations: Dict[int, List[dict]] = defaultdict(list)
        collateral_claim_remaining: Dict[int, Decimal] = {
            relation["mtn"]: quantize_money(reserved_by_setting_mtn[stup_no].get(relation["mtn"], Decimal("0")))
            for relation in summary["relations"]
        }

        for claim in claim_rows:
            allocated_amount = quantize_money(min(remaining_amount, claim["arr_amt"]))
            shortfall_amount = quantize_money(claim["arr_amt"] - allocated_amount)
            remaining_amount = quantize_money(remaining_amount - allocated_amount)

            claim_collateral_allocations: List[dict] = []
            alloc_left = allocated_amount

            ordered_relations = sorted(summary["relations"], key=lambda row: (row["stup_ord"], row["mtn"]))
            available_buckets = [
                {
                    "mtn": relation["mtn"],
                    "stup_ord": relation["stup_ord"],
                    "available": collateral_claim_remaining.get(relation["mtn"], Decimal("0")),
                }
                for relation in ordered_relations
                if collateral_claim_remaining.get(relation["mtn"], Decimal("0")) > 0
            ]

            for bucket in sorted(
                available_buckets,
                key=lambda bucket: (bucket["stup_ord"], bucket["mtn"]),
            ):
                if alloc_left <= 0:
                    break
                take_amount = quantize_money(min(bucket["available"], alloc_left))
                if take_amount <= 0:
                    continue
                collateral_claim_remaining[bucket["mtn"]] = quantize_money(
                    collateral_claim_remaining[bucket["mtn"]] - take_amount
                )
                alloc_left = quantize_money(alloc_left - take_amount)
                claim_collateral_allocations.append(
                    {
                        "mtn": bucket["mtn"],
                        "stup_ord": bucket["stup_ord"],
                        "allocated_amount": float(take_amount),
                    }
                )
                collateral_claim_allocations[bucket["mtn"]].append(
                    {
                        "icn": claim["icn"],
                        "allocated_amount": float(take_amount),
                    }
                )

            allocations.append(
                {
                    "stup_no": stup_no,
                    "icn": claim["icn"],
                    "mrtg_arr_srn": claim["mrtg_arr_srn"],
                    "rgsn_ymd": claim["rgsn_ymd"],
                    "arr_scd": claim["arr_scd"],
                    "edps_csn": claim["edps_csn"],
                    "arr_amt": float(claim["arr_amt"]),
                    "allocated_amount": float(allocated_amount),
                    "shortfall_amount": float(shortfall_amount),
                    "sun_credit_amount": float(shortfall_amount),
                    "collateral_allocations": claim_collateral_allocations,
                }
            )

        collateral_final_reviews = []
        collateral_reserved_breakdown = []

        for relation in sorted(summary["relations"], key=lambda row: (row["stup_ord"], row["mtn"])):
            mtn = relation["mtn"]
            review_row = final_review_by_mtn[mtn]
            reserved_amount = quantize_money(reserved_by_setting_mtn[stup_no].get(mtn, Decimal("0")))

            review_copy = dict(review_row)
            review_copy["reserved_for_setting_amount"] = float(reserved_amount)
            review_copy["remaining_after_network_allocation"] = float(collateral_remaining[mtn])
            review_copy["claim_allocations"] = collateral_claim_allocations.get(mtn, [])
            collateral_final_reviews.append(review_copy)

            collateral_reserved_breakdown.append(
                {
                    "mtn": mtn,
                    "stup_ord": relation["stup_ord"],
                    "final_review_amount": float(to_decimal(review_row["final_review_amount"])),
                    "reserved_for_setting_amount": float(reserved_amount),
                    "network_collateral_remaining": float(collateral_remaining[mtn]),
                }
            )

        collateral_final_review_total = quantize_money(
            sum(to_decimal(row["final_review_amount"]) for row in collateral_final_reviews)
        )
        bnd_max_amt = quantize_money(summary["bnd_max_amt"])
        setting_available_amount = quantize_money(summary["bnd_max_amt"] / SETTING_COVERAGE_RATIO)

        results_by_stup[stup_no] = {
            "stup_no": stup_no,
            "bnd_max_amt": float(bnd_max_amt),
            "setting_available_amount": float(setting_available_amount),
            "collateral_final_review_total": float(collateral_final_review_total),
            "shared_collateral_usable_amount": float(reserved_total),
            "distributable_amount": float(reserved_total),
            "remaining_amount": float(remaining_amount),
            "priority_rule": "Claims: RGSN_YMD asc, then ICN asc",
            "shared_collateral_priority_rule": "Shared collateral: STUP_YMD asc, STUP_ORD asc, then STUP_NO asc",
            "allocations": allocations,
            "collateral_final_reviews": collateral_final_reviews,
            "collateral_reserved_breakdown": collateral_reserved_breakdown,
        }

    return {
        "target_stup_no": target_stup_no,
        "results_by_stup": results_by_stup,
        "reservation_trace": reservation_trace,
        "network_collateral_totals": [
            {
                "mtn": mtn,
                "final_review_amount": float(collateral_original[mtn]),
                "remaining_after_network_allocation": float(collateral_remaining[mtn]),
            }
            for mtn in sorted(collateral_original)
        ],
    }


def compute_allocation_by_stup(db_config: Dict[str, object], stup_no: int) -> dict:
    network_result = compute_network_allocation(db_config, stup_no)
    result = network_result["results_by_stup"].get(stup_no)
    if not result:
        raise ValueError(f"STUP_NO {stup_no} not found")

    return {
        **result,
        "network_target_stup_no": network_result["target_stup_no"],
        "network_collateral_totals": network_result["network_collateral_totals"],
        "reservation_trace": network_result["reservation_trace"],
    }
