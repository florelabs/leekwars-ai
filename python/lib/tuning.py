# Réglages de comportement (les seuls boutons à tourner) + profileur d'opérations par phase.
# (nommé tuning.py : `profile` est un module de la stdlib, qui prime sur les fichiers de l'IA.)

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Profile:
    # Pondérations, toutes en PV : score = valeur − w_safety × danger + w_kill × kills − w_tp_reserve × téléport.
    w_safety: float = 1.0  # 1 = un PV reçu vaut un PV infligé ; > 1 prudent, < 1 agressif
    w_kill: float = 150.0  # bonus fixe par ennemi tué (en PV)
    w_low_life: float = 0.5  # focus : dégâts sur une cible à 0 % de vie valent (1 + w_low_life) fois plus
    w_tp_reserve: float = 30.0  # malus d'utilisation de la téléportation (cooldown 10)
    w_pressure: float = 0.5  # valeur des dégâts que JE pourrais infliger au prochain tour depuis la case finale
    w_stack: float = 0.6  # rendement de chaque bouclier supplémentaire posé le même tour (en garder pour plus tard)
    w_cover: float = 4.0  # PV par obstacle adjacent à la case finale (proxy « cachette ») dans le choix du repli
    poison_discount: float = 0.7  # valeur des tours futurs d'un poison (géométrique)
    future_discount: float = 0.7  # valeur d'un tour futur pour les buffs multi-tours (protéine, boucliers)
    # Bornes de recherche.
    max_stops: int = 2  # arrêts (cases d'où on agit) par tour ; 1 = move → act → move
    beam: int = 8  # séquences conservées par profondeur
    k_walk: int = 6  # cases candidates à pied par objectif
    k_tp: int = 2  # cases candidates par téléportation par objectif
    budget: float = 0.7  # part de System.maxOperations au-delà de laquelle on arrête de chercher
    refine_plans: int = 3  # plans finalistes dont on recalcule le danger avec la vraie LOS ennemie (0 = jamais)
    refine_cells: int = 3  # cases couvertes testées par finaliste, en plus de sa case finale
    refine_los: int = 40  # appels lineOfSight max par case testée (au-delà : pessimiste)
    debug: bool = False


class Phases:
    """Mesure le coût en opérations de chaque phase : `with phases.phase("danger"): ...`."""

    def __init__(self, ops: Callable[[], int]) -> None:
        self.ops = ops
        self.spent: dict[str, int] = {}

    def phase(self, name: str) -> "_Phase":
        return _Phase(self, name)

    def summary(self) -> str:
        total = sum(self.spent.values())
        parts = [f"{k}={v // 1000}k" for k, v in self.spent.items()]
        return f"ops {total // 1000}k : " + " ".join(parts)


class _Phase:
    def __init__(self, phases: Phases, name: str) -> None:
        self.phases = phases
        self.name = name
        self.start = 0

    def __enter__(self) -> None:
        self.start = self.phases.ops()

    def __exit__(self, *exc: object) -> None:
        self.phases.spent[self.name] = self.phases.spent.get(self.name, 0) + self.phases.ops() - self.start
