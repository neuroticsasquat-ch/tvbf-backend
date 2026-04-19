def make_show(show_id: int, updated: int, seasons: int = 1, episodes_per_season: int = 2) -> dict:
    seasons_list = [
        {
            "id": show_id * 1000 + s,
            "number": s,
            "name": f"S{s}",
            "episodeOrder": episodes_per_season,
        }
        for s in range(1, seasons + 1)
    ]
    episodes_list = []
    counter = 0
    for s in range(1, seasons + 1):
        for n in range(1, episodes_per_season + 1):
            counter += 1
            episodes_list.append(
                {
                    "id": show_id * 10000 + counter,
                    "season": s,
                    "number": n,
                    "name": f"S{s}E{n}",
                }
            )
    return {
        "id": show_id,
        "name": f"Show {show_id}",
        "type": "Scripted",
        "updated": updated,
        "genres": ["Drama"],
        "network": None,
        "webChannel": None,
        "_embedded": {"seasons": seasons_list, "episodes": episodes_list},
    }
