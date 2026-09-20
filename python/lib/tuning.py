# Réglages de comportement (les seuls boutons à tourner) + profileur d'opérations par phase.
# (nommé tuning.py : `profile` est un module de la stdlib, qui prime sur les fichiers de l'IA.)

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Profile:
    # Pondérations, toutes en PV : score = valeur − w_safety × danger + w_kill × kills − w_tp_reserve × téléport.
    w_safety: float = 1.0  # 1 = un PV reçu vaut un PV infligé ; > 1 prudent, < 1 agressif
    w_kill: float = 150.0  # bonus fixe par ennemi tué (en PV), + son alpha sur les tours futurs
    w_death: float = 1000.0  # malus si les dégâts attendus sur la case finale peuvent me tuer
    lethal_margin: float = 0.8  # létal si dégâts attendus (après mes protections) ≥ marge × PV
    w_low_life: float = 0.5  # focus : dégâts sur une cible à 0 % de vie valent (1 + w_low_life) fois plus
    w_threat: float = 0.5  # priorité à ce qui fait mal : × (1 + w_threat × alpha(e) / alpha max)
    w_summon: float = 0.4  # multiplicateur des dégâts sur une invocation (bulbe)
    w_finish: float = 1.3  # multiplicateur si je peux le tuer ce tour (vie ≤ mon alpha sur lui)
    w_focus: float = 1.2  # multiplicateur sur la cible principale du tour précédent (persistance)
    w_ally: float = 1.0  # valeur d'un PV soigné / protégé / gagné sur un allié, relativement à moi
    ally_weights: dict[str, float] = field(default_factory=dict)  # par nom d'allié (carry : 1.5, bulbe : 0.3)
    w_team_focus: float = 1.3  # multiplicateur sur la cible annoncée par l'équipe (canal)
    w_summon_value: float = 1.0  # valeur d'une invocation : contribution du bulbe (dégâts / soins) × ce facteur
    summon_turns: int = 2  # tours futurs comptés pour un bulbe (décotés), en plus de son tour immédiat
    engage_share: float = 0.3  # j'annonce l'engagement si mon plan vaut ≥ cette part de mon alpha, ou tue
    poison_cap: float = 0.5  # au-delà de cette part des PV de la cible déjà en poison…
    w_poison_overflow: float = 0.25  # …un poison supplémentaire ne vaut plus que ça (antidote probable)
    w_tp_reserve: float = 30.0  # malus d'utilisation de la téléportation (cooldown 10)
    w_pressure: float = 0.5  # valeur des dégâts que JE pourrais infliger au prochain tour depuis la case finale
    w_stack: float = 0.6  # rendement de chaque bouclier supplémentaire posé le même tour (en garder pour plus tard)
    w_cover: float = 4.0  # PV par obstacle adjacent à la case finale (proxy « cachette ») dans le choix du repli
    poison_discount: float = 0.7  # valeur des tours futurs d'un poison (géométrique)
    w_nova: float = 0.4  # valeur de la vie max retirée au-delà des PV courants (plafonne ses soins)
    # Anti-immobilisme : un ennemi qui ne bouge pas et ne blesse personne voit son danger décoté ; et le
    # temps qui passe rend agressif (un match nul au tour 64 n'est pas une victoire).
    idle_discount: float = 0.6  # facteur par tour d'inactivité observé au-delà du premier (plancher 0.1)
    clock_start: int = 32  # à partir de ce tour, w_safety décroît linéairement…
    clock_min: float = 0.3  # …jusqu'à cette part de sa valeur au dernier tour (MAX_TURNS = 64)
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
