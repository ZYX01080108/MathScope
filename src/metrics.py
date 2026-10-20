from math import log2
def validate_k(k):
    if type(k) is not int or k < 1:
        raise ValueError("k 必须是正整数")


def recall_at_k(ranked_ids, relevant_ids, k):
    validate_k(k)
    relevant = set(relevant_ids)

    if not relevant:
        return None

    retrieved = set(ranked_ids[:k])
    return len(retrieved & relevant) / len(relevant)


def reciprocal_rank_at_k(ranked_ids, relevant_ids, k):
    validate_k(k)
    relevant = set(relevant_ids)

    if not relevant:
        return None

    for rank, item_id in enumerate(ranked_ids[:k], start=1):
        if item_id in relevant:
            return 1 / rank

    return 0.0
def ndcg_at_k(ranked_ids, relevant_ids, k):
    validate_k(k)
    relevant = set(relevant_ids)

    if not relevant:
        return None

    dcg = 0.0
    seen = set()

    for rank, item_id in enumerate(ranked_ids[:k], start=1):
        if item_id in relevant and item_id not in seen:
            dcg += 1 / log2(rank + 1)

        seen.add(item_id)

    ideal_count = min(k, len(relevant))
    ideal_dcg = sum(
        1 / log2(rank + 1)
        for rank in range(1, ideal_count + 1)
    )

    return dcg / ideal_dcg