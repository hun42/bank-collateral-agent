from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List

import psycopg


TWOPLACES = Decimal("0.01")
HUNDRED = Decimal("100")


def to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def get_final_review_amounts_by_stup(db_config: Dict[str, object], stup_no: int) -> List[dict]:
    sql = """
        SELECT
            m52."STUP_NO",
            m52."MTN",
            m52."SBSD_OBJ_SRN",
            m09."MRTG_KCD",
            m09."ETC_SBTC_AMT",
            m09."MRAK_RTO",
            m10."APRS_PRC",
            m10."RSSM_APRS_AMT",
            m10."VLTG_EX_AMT"
        FROM public."TB_CBK_LNM052R" m52
        JOIN public."TB_CBK_LNM009M" m09
            ON m52."MTN" = m09."MTN"
        JOIN public."TB_CBK_LNM010L" m10
            ON m52."MTN" = m10."MTN"
           AND m52."SBSD_OBJ_SRN" = m10."SBSD_OBJ_SRN"
        WHERE m52."STUP_NO" = %s
        ORDER BY m52."MTN", m52."SBSD_OBJ_SRN";
    """

    with psycopg.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (stup_no,))
            rows = cur.fetchall()

    grouped: Dict[int, dict] = {}

    for row in rows:
        mtn = int(row[1])
        sbsd_obj_srn = int(row[2])
        mrtg_kcd = row[3]
        etc_sbtc_amt = to_decimal(row[4])
        mrak_rto = to_decimal(row[5])
        aprs_prc = to_decimal(row[6])
        rssm_aprs_amt = to_decimal(row[7])
        vltg_ex_amt = to_decimal(row[8])

        adopted_base_amount = rssm_aprs_amt if rssm_aprs_amt > 0 else aprs_prc
        net_object_amount = adopted_base_amount - vltg_ex_amt

        if mtn not in grouped:
            grouped[mtn] = {
                "stup_no": int(stup_no),
                "mtn": mtn,
                "mrtg_kcd": mrtg_kcd,
                "etc_sbtc_amt": quantize_money(etc_sbtc_amt),
                "mrak_rto": quantize_money(mrak_rto),
                "object_rows": [],
                "object_sum_before_deduction": Decimal("0"),
            }

        grouped[mtn]["object_rows"].append(
            {
                "sbsd_obj_srn": sbsd_obj_srn,
                "aprs_prc": quantize_money(aprs_prc),
                "rssm_aprs_amt": quantize_money(rssm_aprs_amt),
                "vltg_ex_amt": quantize_money(vltg_ex_amt),
                "adopted_base_amount": quantize_money(adopted_base_amount),
                "net_object_amount": quantize_money(net_object_amount),
            }
        )
        grouped[mtn]["object_sum_before_deduction"] += net_object_amount

    results: List[dict] = []

    for mtn, item in grouped.items():
        object_sum = quantize_money(item["object_sum_before_deduction"])
        after_etc_deduction = quantize_money(object_sum - item["etc_sbtc_amt"])
        final_review_amount = quantize_money(after_etc_deduction * (item["mrak_rto"] / HUNDRED))

        results.append(
            {
                "stup_no": item["stup_no"],
                "mtn": mtn,
                "mrtg_kcd": item["mrtg_kcd"],
                "object_sum_before_deduction": float(object_sum),
                "etc_sbtc_amt": float(item["etc_sbtc_amt"]),
                "after_etc_deduction": float(after_etc_deduction),
                "mrak_rto": float(item["mrak_rto"]),
                "final_review_amount": float(final_review_amount),
                "object_rows": [
                    {
                        **obj,
                        "aprs_prc": float(obj["aprs_prc"]),
                        "rssm_aprs_amt": float(obj["rssm_aprs_amt"]),
                        "vltg_ex_amt": float(obj["vltg_ex_amt"]),
                        "adopted_base_amount": float(obj["adopted_base_amount"]),
                        "net_object_amount": float(obj["net_object_amount"]),
                    }
                    for obj in item["object_rows"]
                ],
            }
        )

    return sorted(results, key=lambda x: x["mtn"])
