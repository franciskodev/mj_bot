from config import DEFAULT_FAMILY_RANK, FAMILY_RANK_ROLE_IDS, RECRUITMENT_POSITION_ROLE_IDS


def family_rank_from_roles(role_ids: set[int]) -> str:
    return next(
        (rank for rank, role_id in reversed(FAMILY_RANK_ROLE_IDS.items()) if role_id in role_ids),
        DEFAULT_FAMILY_RANK,
    )


def recruitment_positions_from_roles(
    role_ids: set[int], recruiter_role_ids: set[int]
) -> list[str]:
    positions = ["Рекрутёр"] if role_ids & recruiter_role_ids else []
    positions.extend(
        name for name, role_id in RECRUITMENT_POSITION_ROLE_IDS.items()
        if role_id in role_ids
    )
    return positions
