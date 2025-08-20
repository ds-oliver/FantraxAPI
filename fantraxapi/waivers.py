from typing import Optional, Dict, Any, List

from .exceptions import FantraxException


class WaiversService:
    """Feature module for waiver/claim operations."""

    _DISPLAYED_MISC_DISPLAY_TYPE = "1"
    _DISPLAYED_SCORING_CATEGORY_TYPE = "5"
    _DEFAULT_VIEW = "STATS"

    def __init__(self, request_callable, api):
        self._request = request_callable
        self._api = api

    # ---------- UI-identical list call ----------
    def _fetch_player_stats_page(
        self,
        *,
        page_number: int = 1,
        status: str = "ALL_AVAILABLE",
        pos_or_group: Optional[str] = None,
        max_results: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Call the exact private method the Players page uses:
          method: getPlayerStats
          data keys:
            - statusOrTeamFilter
            - pageNumber
            - (optional) posOrGroup + displayedPosOrGroup
            - displayedView, displayedMiscDisplayType, displayedScoringCategoryType
        """
        data: Dict[str, Any] = {
            "statusOrTeamFilter": status,
            "pageNumber": str(page_number),
            "displayedView": self._DEFAULT_VIEW,
            "displayedMiscDisplayType": self._DISPLAYED_MISC_DISPLAY_TYPE,
            "displayedScoringCategoryType": self._DISPLAYED_SCORING_CATEGORY_TYPE,
        }
        # IMPORTANT: for "All players" the UI does NOT send posOrGroup at all.
        if pos_or_group:
            data["posOrGroup"] = pos_or_group
            data["displayedPosOrGroup"] = pos_or_group
        if max_results:
            data["maxResultsPerPage"] = str(max_results)

        return self._request("getPlayerStats", **data)

    # ---------- Submit (UI-mirroring flow) ----------
    def submit_claim(
        self,
        *,
        team_id: str,
        claim_scorer_id: str,
        bid_amount: float = 0.0,
        drop_scorer_id: Optional[str] = None,
        to_position_id: Optional[str] = None,
        to_status_id: str = "2",  # 1=Active, 2=Reserve/Bench
        group: Optional[int] = None,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Submit a waiver claim by mirroring the UI flow:
          1. getScorerDetails - Initial player info
          2. getClaimDropConfirmInfo - Pre-submit validation
          3. createClaimDrop - The actual claim submission
        """
        try:
            # 1. Get initial player info
            self._request(
                "getScorerDetails",
                scorers=[{"scorerId": claim_scorer_id, "action": "CLAIM"}],
                teamId=team_id,
            )

            # 2. Pre-submit validation
            tx = {
                "type": "CLAIM",
                "scorerId": claim_scorer_id,
                "teamId": team_id,
            }
            if bid_amount:
                tx["bid"] = str(bid_amount)

            self._request(
                "getClaimDropConfirmInfo",
                transactionSets=[{"transactions": [tx]}],
            )

            # 3. Submit the claim
            tx_submit = {
                "type": "CLAIM",
                "scorerId": claim_scorer_id,
                "teamId": team_id,
                "positionId": to_position_id,
                "claimToStatusId": to_status_id,
                "doConfirm": True,
            }
            if bid_amount:
                tx_submit["bid"] = bid_amount

            if drop_scorer_id:
                tx_submit["dropScorerId"] = drop_scorer_id

            if priority is not None:
                tx_submit["priority"] = priority
            if group is not None:
                tx_submit["group"] = group

            return self._request(
                "createClaimDrop",
                transactionSets=[{"transactions": [tx_submit]}],
            )

        except FantraxException as e:
            raise FantraxException(f"Failed to submit claim: {e}")

    # ---------- Search / Browse ----------
    def search_players(
        self,
        query: str,
        pos_or_group: Optional[str] = None,
        max_results: int = 25,
        status: str = "ALL_AVAILABLE",
    ) -> List[Dict[str, Any]]:
        # 1) Try server-side search via getPlayerStats
        try:
            per_page = max(1, min(50, max_results))
            resp = self._request(
                "getPlayerStats",
                statusOrTeamFilter=status,
                pageNumber="1",
                displayedView=self._DEFAULT_VIEW,
                displayedMiscDisplayType=self._DISPLAYED_MISC_DISPLAY_TYPE,
                displayedScoringCategoryType=self._DISPLAYED_SCORING_CATEGORY_TYPE,
                **({"posOrGroup": pos_or_group, "displayedPosOrGroup": pos_or_group} if pos_or_group else {}),
                query=query,
                searchName=query,
                maxResultsPerPage=str(per_page),
            )
            players: List[Dict[str, Any]] = []
            data_block = resp if isinstance(resp, dict) else {}
            table = data_block.get("statsTable") or data_block.get("table") or []
            rows = table if isinstance(table, list) else table.get("rows", [])
            for row in rows:
                scorer = row.get("scorer") or row.get("player") or {}
                pid = scorer.get("scorerId")
                name = scorer.get("name") or scorer.get("shortName") or scorer.get("longName")
                if not (pid and name):
                    continue
                players.append({
                    "id": pid,
                    "name": name,
                    "team": scorer.get("teamShortName") or scorer.get("teamName"),
                    "position": scorer.get("posShortNames"),
                    "default_pos_id": scorer.get("defaultPosId"),
                })
            if players:
                return players[:max_results]
        except FantraxException:
            pass

        # 2) Client-side filter via paging
        results: List[Dict[str, Any]] = []
        page_number = 1
        try:
            while len(results) < max_results and page_number <= 10:
                per_page = max(1, min(50, max_results - len(results)))
                resp = self._fetch_player_stats_page(
                    page_number=page_number,
                    status=status,
                    pos_or_group=pos_or_group,
                    max_results=per_page,
                )
                data_block = resp if isinstance(resp, dict) else {}
                table = data_block.get("statsTable") or data_block.get("table") or []
                rows = table if isinstance(table, list) else table.get("rows", [])
                if not rows:
                    break
                q = (query or "").lower()
                for row in rows:
                    scorer = row.get("scorer") or row.get("player") or {}
                    name = scorer.get("name") or scorer.get("shortName") or scorer.get("longName")
                    if not name or (q and q not in name.lower()):
                        continue
                    pid = scorer.get("scorerId")
                    if not pid:
                        continue
                    results.append({
                        "id": pid,
                        "name": name,
                        "team": scorer.get("teamShortName") or scorer.get("teamName"),
                        "position": scorer.get("posShortNames"),
                        "default_pos_id": scorer.get("defaultPosId"),
                    })
                    if len(results) >= max_results:
                        break
                prs = data_block.get("paginatedResultSet") or {}
                total_pages = prs.get("totalNumPages")
                page_number += 1
                if total_pages and isinstance(total_pages, (int, str)) and page_number > int(total_pages):
                    break
        except FantraxException as e:
            raise FantraxException(f"search_players(getPlayerStats) failed: {e}")

        return results[:max_results]

    def list_players_by_name(
        self,
        limit: int = 50,
        pos_or_group: Optional[str] = None,   # None => do NOT send posOrGroup (UI behavior for "All")
        status: str = "ALL_AVAILABLE",
    ) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        page_number = 1
        last_total_pages: Optional[int] = None

        try:
            while True:
                per_page = max(1, min(50, limit - len(collected)))
                resp = self._fetch_player_stats_page(
                    page_number=page_number,
                    status=status,
                    pos_or_group=pos_or_group,
                    max_results=per_page,
                )

                if not isinstance(resp, dict):
                    break

                data_block = resp
                table = data_block.get("statsTable") or data_block.get("table") or []
                rows = table if isinstance(table, list) else table.get("rows", [])
                if not rows:
                    break

                for row in rows:
                    scorer = row.get("scorer") or row.get("player") or {}
                    pid = scorer.get("scorerId")
                    name = scorer.get("name") or scorer.get("shortName") or scorer.get("longName")
                    if not (pid and name):
                        continue
                    collected.append({
                        "id": pid,
                        "name": name,
                        "team": scorer.get("teamShortName") or scorer.get("teamName"),
                        "position": scorer.get("posShortNames"),
                        "default_pos_id": scorer.get("defaultPosId"),
                    })
                    if len(collected) >= limit:
                        return collected[:limit]

                prs = data_block.get("paginatedResultSet") or {}
                if last_total_pages is None and "totalNumPages" in prs:
                    try:
                        last_total_pages = int(prs["totalNumPages"])
                    except Exception:
                        last_total_pages = None

                page_number += 1
                if last_total_pages and page_number > last_total_pages:
                    break

        except FantraxException as e:
            raise FantraxException(f"list_players_by_name(getPlayerStats) failed: {e}")

        return collected[:limit]