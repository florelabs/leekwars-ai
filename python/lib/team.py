# Canal d'équipe : chaque leek publie après son tour (Network.sendAll, type CUSTOM, params JSON) sa cible
# principale et s'il a engagé ; les suivants dans l'ordre de jeu le lisent au snapshot.
#
# Ce que le canal NE transporte PAS : les poisons déjà posés (l'état réel est lisible sur `enemy.effects`,
# cf `Ent.poison_load`), les positions (lisibles), les dégâts prévus (l'allié a déjà joué quand je lis).
#
# Module pur : le JSON entre et sort, l'API est lue/écrite dans world.py et executor.py.

import json
from dataclasses import dataclass


@dataclass
class Report:
    author: int
    turn: int
    focus: int | None  # id de l'ennemi principal
    engage: bool  # a frappé / frappe ce tour : le combat est lancé


class TeamState:
    def __init__(self, turn: int) -> None:
        self.turn = turn
        self.reports: dict[int, Report] = {}  # dernier rapport par auteur

    def add(self, author: int, params: object) -> None:
        try:
            d = json.loads(params) if isinstance(params, str) else params
            if not isinstance(d, dict):
                return
            r = Report(author, int(d.get("t", 0)), d.get("f"), bool(d.get("e", 0)))
        except (ValueError, TypeError):
            return
        if r.turn < self.turn - 1:
            return  # trop vieux (un rapport vaut pour le tour courant et le suivant)
        prev = self.reports.get(author)
        if prev is None or r.turn >= prev.turn:
            self.reports[author] = r

    @property
    def engaged(self) -> bool:
        return any(r.engage for r in self.reports.values())

    @property
    def focus(self) -> int | None:
        """Cible d'équipe : celle des rapports d'engagement, sinon la plus citée."""
        votes: dict[int, float] = {}
        for r in self.reports.values():
            if r.focus is not None:
                votes[r.focus] = votes.get(r.focus, 0.0) + (3.0 if r.engage else 1.0)
        return max(votes, key=lambda k: votes[k]) if votes else None

    @staticmethod
    def encode(turn: int, focus: int | None, engage: bool) -> str:
        return json.dumps({"t": turn, "f": focus, "e": 1 if engage else 0})
