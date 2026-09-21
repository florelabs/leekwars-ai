# Planificateur : un tour = contextes structurels × séquences d'arrêts × sac à dos de PT × repli.
#
#   contextes  : base, puis chaque buff sur moi (+force, +PM, +PT) — un World dérivé par contexte
#   arrêts     : cases d'où agir, générées depuis les CIBLES (pattern de portée autour de chaque ennemi),
#                atteintes à pied ou par téléportation (arête à 9 PT, une fois par tour)
#   séquences  : beam search sur ≤ max_stops arrêts sous budget PM, bornée par une borne sup et par les ops
#   sac à dos  : chaque skill est utilisé à UN arrêt (n usages), DP « une option par groupe » sur les PT
#   variantes  : une entrave PM/PT sur un ennemi change sa map de danger → on évalue avec et sans
#   repli      : case finale minimisant le danger avec les PM restants (ou par téléportation si inutilisée)
#
# Anytime : le plan « rester + tirer » existe dès la première évaluation ; tout le reste n'est exploré que
# si `System.operations` reste sous `profile.budget`.

from dataclasses import dataclass, field, replace

import formulas
from danger import BASE, SHACKLED_MP, SHACKLED_TP, Danger, damage_by_range, unit_damage, unit_raw
from geometry import Grid, bfs_walk
from skills import (
    ABS_SHIELD,
    ATTACKS,
    BOOST_LIFE,
    BUFF_MP,
    BUFF_TP,
    DAMAGE,
    HEAL,
    POISON,
    REL_SHIELD,
    SELF_CONTEXT,
    SHACKLE_MP,
    SHACKLE_TP,
    STAT_BUFFS,
    STAT_OF_KIND,
    SUMMON,
    SUPPORT,
    TELEPORT,
    Skill,
)
from tuning import Profile
from world import Ent, World

WALK = "walk"
STAY = "stay"
VIA_TELEPORT = "teleport"
HITS_PER_TURN = 3  # nombre d'attaques ennemies estimé pour valoriser un bouclier absolu

# Cible principale du tour précédent (les globales survivent au tour) : persistance du focus.
focus: dict[str, int | None] = {"target": None}


@dataclass
class Action:
    kind: str  # "move" | "teleport" | "weapon" | "chip"
    cell: int | None = None  # move / teleport : case d'arrivée
    skill: Skill | None = None
    target: int | None = None  # id d'entité visée (weapon / chip)
    n: int = 1

    def __str__(self) -> str:
        if self.kind in ("move", "teleport"):
            return f"{self.kind}→{self.cell}"
        if self.kind == "summon":
            return f"{self.skill.key if self.skill else '?'}→{self.cell}"
        return f"{self.skill.key if self.skill else '?'}×{self.n}@{self.target}"


@dataclass
class Plan:
    actions: list[Action]
    score: float
    value: float = 0.0
    danger: float = 0.0
    end_cell: int = -1
    kills: list[int] = field(default_factory=list)
    note: str = ""
    # Contexte de construction (pour recalculer le repli avec le danger raffiné) — interne.
    seq: "Seq | None" = None
    me: Ent | None = None
    prefix: list[Action] = field(default_factory=list)
    chosen: list[tuple] = field(default_factory=list)
    alive: dict[int, tuple[str, float]] = field(default_factory=dict)
    end_via: str = "walk"
    pressure: float = 0.0
    lethal: bool = False
    rel_mult: float = 1.0  # Π(1 − bouclier relatif) posés ce tour
    abs_block: float = 0.0  # boucliers absolus posés ce tour × coups
    life_after: float = 0.0  # PV après soins du tour

    def describe(self) -> str:
        acts = " ; ".join(str(a) for a in self.actions)
        flag = " LÉTAL" if self.lethal else ""
        return (f"score={self.score:.0f} value={self.value:.0f} danger={self.danger:.0f}{flag} "
                f"kills={self.kills} [{acts}] {self.note}")


@dataclass(frozen=True)
class Stop:
    cell: int
    via: str
    mp_cost: int = 0
    tp_cost: int = 0


@dataclass
class Seq:
    stops: list[Stop]
    mp_left: int
    tp_left: int
    teleport_used: bool
    ub: float = 0.0

    @property
    def last(self) -> int:
        return self.stops[-1].cell


class Planner:
    def __init__(self, world: World, profile: Profile, danger: Danger | None = None) -> None:
        self.w = world
        self.p = self.clocked(profile, world.turn)
        self.d = danger or Danger(world, profile.poison_discount, profile.idle_discount)
        self.grid: Grid = world.grid
        self._reach: dict[tuple[int, int], dict[int, int]] = {}
        self._objective: dict[tuple, list[float]] = {}
        self._hit: dict[tuple[int, str, int], bool] = {}
        self._tp_ring: dict[int, list[int]] = {}
        self.evaluations = 0
        self.stats = ""  # diagnostic de la dernière recherche (cases atteignables / candidates / borne max)
        self.finalists: list[Plan] = []
        self.ally_w: dict[int, float] = {a.id: profile.w_ally * profile.ally_weights.get(a.name, 1.0)
                                         for a in world.allies}
        self.target_w: dict[int, float] = self.target_weights()
        self.tp_skill: Skill | None = None
        for s in world.me.skills:
            if s.kind == TELEPORT and s.available and s.cost <= world.me.tp and not world.teleport_used:
                self.tp_skill = s

    @staticmethod
    def clocked(profile: Profile, turn: int) -> Profile:
        """Horloge : passé `clock_start`, w_safety décroît linéairement jusqu'à `clock_min × w_safety` au
        tour MAX_TURNS (64). Un match nul n'est pas une victoire."""
        if turn <= profile.clock_start or profile.clock_min >= 1.0:
            return profile
        span = max(1, 64 - profile.clock_start)
        f = max(profile.clock_min, 1.0 - (1.0 - profile.clock_min) * (turn - profile.clock_start) / span)
        return replace(profile, w_safety=profile.w_safety * f)

    # ---- menace d'équipe ---------------------------------------------------------------------------

    def threat(self, e: Ent) -> float:
        """Ce que `e` peut infliger en un tour à sa meilleure cible dans MON équipe : max(alpha sur moi,
        alpha sur chaque allié × son poids). Sert au bonus de kill et à la priorisation."""
        cache = self.__dict__.setdefault("_threat", {})
        t = cache.get(e.id)
        if t is None:
            t = self.d.alpha(e)
            for a in self.w.allies:
                v = self.ally_w.get(a.id, 0.0) * self.d.alpha_vs(e, a)
                if v > t:
                    t = v
            cache[e.id] = t
        return t

    def ally_relief(self, alive_base: dict[int, tuple[str, float]], alive: dict[int, tuple[str, float]]) -> float:
        """Danger retiré à mes alliés (pondéré) par une entrave ou un kill : Σ_a w_a × (danger_vs base − après)."""
        total = 0.0
        for a in self.w.allies:
            wa = self.ally_w.get(a.id, 0.0)
            if wa <= 0:
                continue
            total += wa * (self.d.danger_vs(a, a.cell, alive_base) - self.d.danger_vs(a, a.cell, alive))
        return total

    # ---- invocations -------------------------------------------------------------------------------

    def summon_options(self, sk: Skill, stops: list[Stop]) -> list[tuple[int, float, tuple]]:
        """Une option par arrêt : la meilleure case d'apparition à portée de la puce (vide, LOS)."""
        w = self.w
        proto = w.bulbs.get(sk.key)
        if proto is None or w.summon_count >= w.summon_limit:
            return []
        blocked = w.blocked
        opts = []
        for si, st in enumerate(stops):
            best_c, best_v = -1, 0.0
            for c in self.grid.ring(st.cell, sk.min_range, sk.max_range, sk.launch):
                if blocked[c] or (sk.los and not self.w.los(st.cell, c)):
                    continue
                v = self.summon_value(proto, c)
                if v > best_v:
                    best_c, best_v = c, v
            if best_c >= 0:
                opts.append((sk.cost, best_v, (sk, si, best_c, 1, 0.0)))
        return opts

    def summon_value(self, proto: Ent, cell: int) -> float:
        """Contribution attendue d'un bulbe apparaissant en `cell` : ce qu'il peut faire dès son tour (il joue
        juste après moi) + son alpha sur les tours suivants, décoté et pondéré par sa survie."""
        cache = self.__dict__.setdefault("_summon_v", {})
        key = (proto.id, cell)
        v = cache.get(key)
        if v is not None:
            return v
        d = self.d
        grid = self.grid
        p = self.p
        immediate = 0.0
        for e in self.w.enemies:
            dmg = self.__dict__.setdefault("_bulb_dmg", {}).get((proto.id, e.id))
            if dmg is None:
                dmg = damage_by_range(proto, e, proto.max_tp, p.poison_discount)
                self.__dict__["_bulb_dmg"][(proto.id, e.id)] = dmg
            dist = max(0, grid.dist(cell, e.cell) - proto.max_mp)
            if dist < len(dmg) - 1:
                imm = dmg[dist] * self.target_w.get(e.id, 1.0)
                if imm > immediate:
                    immediate = imm
        heal_cap = 0.0
        for hsk in proto.skills:
            if hsk.kind == HEAL and hsk.available:
                heal_cap += hsk.avg if hsk.raw else formulas.heal(hsk.avg, proto.wisdom, proto.power)
        if heal_cap > 0:
            reach = proto.max_mp + max((hsk.max_range for hsk in proto.skills if hsk.kind == HEAL), default=0)
            for a in [self.w.me, *self.w.allies]:
                if grid.dist(cell, a.cell) <= reach:
                    wa = 1.0 if a.id == self.w.me.id else self.ally_w.get(a.id, 0.0)
                    hv = min(heal_cap, a.max_life - a.life) * wa
                    if hv > immediate:
                        immediate = hv
        dmg_alpha = d.alpha_of(proto)
        offensive = dmg_alpha >= heal_cap
        alpha = max(dmg_alpha, heal_cap)
        bulb_danger = d.danger_vs(proto, cell)
        survival = 1.0 if bulb_danger < proto.life * p.lethal_margin else 0.4
        v = p.w_summon_value * (immediate + alpha * self.future_turns(p.summon_turns) * survival)
        if offensive:
            # Devant : ses PV absorbent des PT ennemis (pénalité de danger faible), et chaque case gagnée vers
            # l'ennemi le plus proche compte — sinon toutes les cases « à portée » sont équivalentes et le
            # bulbe apparaît derrière moi.
            v -= p.w_summon_safety * bulb_danger
            v -= p.w_summon_forward * min(grid.dist(cell, e.cell) for e in self.w.enemies)
        else:
            v -= p.w_safety * p.w_summon * bulb_danger  # support : en retrait, ses PV valent ceux d'une invocation
        cache[key] = v
        return v

    # ---- priorisation de cible ---------------------------------------------------------------------

    def target_weights(self) -> dict[int, float]:
        """Multiplicateur de la valeur des dégâts par ennemi, calculé une fois par tour : menace (son alpha
        sur moi), invocation, finissable ce tour, persistance du focus. La composante « blessé »
        (`w_low_life`) reste dans `evaluate` car elle dépend de la vie courante."""
        p = self.p
        enemies = self.w.enemies
        if not enemies:
            return {}
        alphas = {e.id: self.threat(e) for e in enemies}
        max_alpha = max(alphas.values()) or 1.0
        out: dict[int, float] = {}
        for e in enemies:
            w = 1.0 + p.w_threat * alphas[e.id] / max_alpha
            if e.summoned:
                w *= p.w_summon
            if e.life <= self.d.my_alpha_vs(e):
                w *= p.w_finish
            if focus["target"] == e.id:
                w *= p.w_focus
            if self.w.team is not None and self.w.team.focus == e.id:
                w *= p.w_team_focus
            out[e.id] = w
        return out

    # ---- utilitaires -------------------------------------------------------------------------------

    def over_budget(self) -> bool:
        return self.w.ops() > self.w.max_ops * self.p.budget

    def reach_from(self, cell: int, mp: int) -> dict[int, int]:
        key = (cell, mp)
        r = self._reach.get(key)
        if r is None:
            r = bfs_walk(self.grid, {cell: 0}, mp, self.w.blocked_for(self.w.me))
            self._reach[key] = r
        return r

    def can_hit(self, frm: int, sk: Skill, target_cell: int) -> bool:
        key = (frm, sk.key, target_cell)
        v = self._hit.get(key)
        if v is None:
            v = self.grid.in_range(frm, target_cell, sk.min_range, sk.max_range, sk.launch) and \
                (not sk.los or self.w.los(frm, target_cell))
            self._hit[key] = v
        return v

    def teleport_targets(self, frm: int) -> list[int]:
        sk = self.tp_skill
        if sk is None:
            return []
        r = self._tp_ring.get(frm)
        if r is None:
            blocked = self.w.blocked
            r = [c for c in self.grid.ring(frm, sk.min_range, sk.max_range, sk.launch) if not blocked[c]]
            self._tp_ring[frm] = r
        return r

    # ---- contextes ---------------------------------------------------------------------------------

    def contexts(self) -> list[tuple[Ent, list[Action], float]]:
        """(me dérivé, actions préalables, gain futur) — le gain futur est la valeur du buff sur ses tours
        suivants ; `plan()` le pondère par la probabilité d'engagement depuis la case FINALE du plan :
        on se buffe AVANT le contact, pas quand on s'en éloigne."""
        me = self.w.me
        out: list[tuple[Ent, list[Action], float]] = [(me, [], 0.0)]
        for sk in me.skills:
            if sk.kind not in SELF_CONTEXT or not sk.available or sk.cost > me.tp or not sk.can_target(me, True):
                continue  # (férocité : portée 1–8, ne se lance pas sur soi → support sur allié seulement)
            tp = me.tp - sk.cost
            amount = sk.avg if sk.raw else formulas.buff(sk.avg, me.science, me.power)
            bonus = 0.0
            if sk.kind in STAT_BUFFS:
                stat = STAT_OF_KIND[sk.kind]
                me2 = replace(me, tp=tp, **{stat: getattr(me, stat) + int(amount)})
                if sk.turns > 1:
                    bonus = self.stat_gain(sk, me) * self.future_turns(sk.turns - 1)
            elif sk.kind == BUFF_MP:
                me2 = replace(me, tp=tp, mp=me.mp + round(amount))
            elif sk.kind == BUFF_TP:
                me2 = replace(me, tp=tp + round(amount))
            else:
                continue
            out.append((me2, [Action("chip", skill=sk, target=me.id)], bonus))
        return out

    def stat_gain(self, sk: Skill, ent: Ent) -> float:
        """Gain d'alpha (dégâts max par tour) qu'un buff de caractéristique lancé par moi apporte à `ent`
        (moi ou un allié), caché par (skill, stats de la cible)."""
        key = (sk.key, ent.id, ent.strength, ent.magic, ent.agility, ent.power, ent.max_tp)
        cache = self.__dict__.setdefault("_gain", {})
        g = cache.get(key)
        if g is None:
            me = self.w.me
            stat = STAT_OF_KIND[sk.kind]
            amount = sk.avg if sk.raw else formulas.buff(sk.avg, me.science, me.power)
            ent2 = replace(ent, **{stat: getattr(ent, stat) + int(amount)})
            g = max(0.0, self.d.alpha_of(ent2) - self.d.alpha_of(ent))
            cache[key] = g
        return g

    def future_turns(self, n: int) -> float:
        """Poids cumulé de n tours futurs (géométrique, `future_discount`)."""
        total = 0.0
        w = self.p.future_discount
        for _ in range(n):
            total += w
            w *= self.p.future_discount
        return total

    # ---- candidats ---------------------------------------------------------------------------------

    def waypoints(self, me: Ent, reach0: dict[int, int]) -> tuple[list[int], dict[int, float]]:
        """Cases utiles (d'où au moins un skill offensif touche un ennemi) et leur borne sup de valeur."""
        offensive = [s for s in me.skills if s.kind in (*ATTACKS, SHACKLE_MP, SHACKLE_TP)
                     and s.available and s.cost <= me.tp]
        tp_ring = set(self.teleport_targets(me.cell))
        blocked = self.w.blocked
        ub: dict[int, float] = {}
        per_enemy: list[tuple[list[int], list[int]]] = []
        for e in self.w.enemies:
            walk_cands: dict[int, float] = {}
            tp_cands: dict[int, float] = {}
            for sk in offensive:
                if sk.kind in ATTACKS:
                    v = unit_damage(sk, me, e, self.p.poison_discount, self.p.w_nova) * sk.uses_cap(me.tp)
                else:
                    v = self.p.w_safety * 0.25 * self.d.at(e.cell)  # entrave : ordre de grandeur, pas une borne
                if v <= 0:
                    continue
                for c in self.grid.ring(e.cell, sk.min_range, sk.max_range, sk.launch):
                    if c in reach0:
                        walk_cands[c] = walk_cands.get(c, 0.0) + v
                    elif c in tp_ring and not blocked[c]:
                        tp_cands[c] = tp_cands.get(c, 0.0) + v
            for cands, k in ((walk_cands, self.p.k_walk), (tp_cands, self.p.k_tp)):
                kept = self.keep(cands, k, lambda c, e=e: any(self.can_hit(c, sk, e.cell) for sk in offensive))
                for c in kept:
                    ub[c] = ub.get(c, 0.0) + cands[c]
                per_enemy.append((kept, []))
        # Support : cases d'où un soin / bouclier / buff atteint un allié (valeur : ce qu'il en tirerait).
        support = [s for s in me.skills if s.kind in SUPPORT and s.available and s.cost <= me.tp]
        for a in self.w.allies:
            wa = self.p.w_ally * self.p.ally_weights.get(a.name, 1.0)
            if wa <= 0:
                continue
            exp_a = self.d.danger_vs(a, a.cell)
            cands: dict[int, float] = {}
            for sk in support:
                if not sk.can_target(a, False):
                    continue
                if sk.kind == HEAL:
                    v = min(self.heal_value(sk, me), a.max_life - a.life)
                elif sk.kind == BOOST_LIFE:
                    v = self.heal_value(sk, me) * self.p.w_boost_life
                elif sk.kind in (ABS_SHIELD, REL_SHIELD):
                    v = self.absorbed(sk, self.shield_value(sk, me), exp_a)
                else:
                    v = self.d.engage(a.cell) * self.stat_gain(sk, a) * self.future_turns(sk.turns)
                v *= wa
                if v <= 0:
                    continue
                for c in self.grid.ring(a.cell, sk.min_range, sk.max_range, sk.launch):
                    if c in reach0 or (c in tp_ring and not blocked[c]):
                        cands[c] = cands.get(c, 0.0) + v
            kept = self.keep(cands, self.p.k_walk,
                             lambda c, a=a: any(self.can_hit(c, sk, a.cell) for sk in support if sk.can_target(a, False)))
            for c in kept:
                ub[c] = ub.get(c, 0.0) + cands[c]
            per_enemy.append((kept, []))
        cells = {me.cell}
        for kept, _ in per_enemy:
            cells.update(kept)
        ub.setdefault(me.cell, 0.0)
        ordered = sorted(cells, key=lambda c: ub[c] - self.p.w_safety * self.d.at(c), reverse=True)
        return ordered, ub

    def keep(self, cands: dict[int, float], k: int, ok) -> list[int]:
        """Garde k cases : les meilleures par `valeur − w_safety × danger` (cases sûres d'abord), en
        garantissant les 2 meilleures par valeur brute (une case dangereuse mais décisive doit être
        évaluée : le score final tranchera). `ok(c)` vérifie la LOS sur les cases retenues seulement."""
        by_rank = sorted(cands, key=lambda c: cands[c] - self.p.w_safety * self.d.at(c), reverse=True)
        by_value = sorted(cands, key=lambda c: cands[c], reverse=True)
        kept: list[int] = []
        for c in by_value[:2]:
            if ok(c):
                kept.append(c)
        for c in by_rank:
            if len(kept) >= k:
                break
            if c not in kept and ok(c):
                kept.append(c)
        return kept

    # ---- recherche ---------------------------------------------------------------------------------

    def plan(self) -> Plan:
        best: Plan | None = None
        for me, prefix, future_gain in self.contexts():
            start = len(self.finalists)
            plan = self.plan_ctx(me, prefix)
            if future_gain > 0:
                for p in self.finalists[start:]:
                    p.score += self.d.engage(p.end_cell) * future_gain
            if plan is not None and (best is None or plan.score > best.score):
                best = plan
            if self.over_budget():
                break
        if best is None:
            return Plan([], 0.0, note="rien à faire")
        # Raffinement : vraie LOS ennemie sur les finalistes (le danger ne peut que baisser → cachettes).
        if self.p.refine_plans > 0 and not self.over_budget():
            finalists = sorted(self.finalists, key=lambda p: p.score, reverse=True)[: self.p.refine_plans]
            for p in finalists:
                self.refine(p)
                if self.over_budget():
                    break
            best = max(finalists, key=lambda p: p.score)
        dealt: dict[int, float] = {}
        for sk, _si, tid, _n, raw in best.chosen:
            if sk.kind in ATTACKS:
                dealt[tid] = dealt.get(tid, 0.0) + max(raw, 1.0)
        focus["target"] = max(dealt, key=lambda t: dealt[t]) if dealt else None
        return best

    def refine(self, plan: Plan) -> None:
        """Recalcule le danger de la case finale avec la vraie LOS, et essaie quelques cases couvertes
        atteignables à la place : si l'ennemi ne peut voir aucune d'elles, « tirer puis se cacher » gagne."""
        seq = plan.seq
        if seq is None or plan.me is None or plan.danger <= 0 or plan.end_via == VIA_TELEPORT:
            return
        d = self.d
        grid = self.grid
        pfield = d.pressure_field()
        field = d.combined(plan.alive)
        ws, wp, wc = self.p.w_safety, self.p.w_pressure, self.p.w_cover
        candidates = [plan.end_cell]
        if seq.mp_left > 0:
            covered = [c for c, cost in self.reach_from(seq.last, seq.mp_left).items()
                       if cost <= seq.mp_left and c != plan.end_cell and field[c] > 0 and grid.cover(c) > 0]
            covered.sort(key=lambda c: (grid.cover(c), -field[c]), reverse=True)
            candidates.extend(covered[: self.p.refine_cells])
        best_c, best_o, best_danger = plan.end_cell, None, plan.danger
        for c in candidates:
            danger = d.refined(c, plan.alive, self.p.refine_los)
            o = ws * danger - wp * pfield[c] - wc * grid.cover(c)
            if best_o is None or o < best_o:
                best_c, best_o, best_danger = c, o, danger
        if best_c != plan.end_cell or best_danger != plan.danger:
            plan.score += ws * (plan.danger - best_danger) + wp * (pfield[best_c] - plan.pressure)
            incoming = max(0.0, best_danger * plan.rel_mult - plan.abs_block)
            lethal = incoming >= plan.life_after * self.p.lethal_margin
            if plan.lethal and not lethal:
                plan.score += self.p.w_death
            plan.lethal = lethal
            plan.danger = best_danger
            plan.pressure = pfield[best_c]
            plan.end_cell = best_c
            plan.actions = self.build_actions(seq, plan.prefix, plan.chosen, best_c, WALK, plan.me)
            plan.note += f" LOS→{best_danger:.0f}"

    def plan_ctx(self, me: Ent, prefix: list[Action]) -> Plan | None:
        reach0 = self.reach_from(me.cell, me.mp)
        cells, ub = self.waypoints(me, reach0)
        self.stats = f"reach={len(reach0)} cells={len(cells) - 1} ub_max={max(ub.values()):.0f}"
        tp_ring = set(self.teleport_targets(me.cell)) if self.tp_skill else set()
        # Le premier arrêt est toujours la case de départ (agir avant de bouger est permis).
        root = Seq([Stop(me.cell, STAY)], me.mp, me.tp, False, ub.get(me.cell, 0.0))
        best = self.evaluate(root, me, prefix, None)
        layer = [root]
        for _depth in range(self.p.max_stops):
            nxt: list[Seq] = []
            for seq in layer:
                frm = seq.last
                reach = reach0 if frm == me.cell else self.reach_from(frm, seq.mp_left)
                for c in cells:
                    if any(s.cell == c for s in seq.stops):
                        continue
                    cost = reach.get(c)
                    if cost is not None and cost <= seq.mp_left:
                        stop = Stop(c, WALK, mp_cost=cost)
                        nxt.append(Seq([*seq.stops, stop], seq.mp_left - cost, seq.tp_left, seq.teleport_used,
                                       seq.ub + ub.get(c, 0.0)))
                    elif self.tp_skill and not seq.teleport_used and c in tp_ring and \
                            seq.tp_left >= self.tp_skill.cost and self.grid.in_range(frm, c, 1, self.tp_skill.max_range):
                        stop = Stop(c, VIA_TELEPORT, tp_cost=self.tp_skill.cost)
                        nxt.append(Seq([*seq.stops, stop], seq.mp_left, seq.tp_left - self.tp_skill.cost, True,
                                       seq.ub + ub.get(c, 0.0)))
            if not nxt:
                break
            nxt.sort(key=lambda s: s.ub, reverse=True)
            layer = nxt[: self.p.beam]
            for seq in layer:
                if self.over_budget():
                    return best
                if best is not None and self.bound(seq) <= best.score:
                    continue
                best = self.evaluate(seq, me, prefix, best)
        return best

    def bound(self, seq: Seq) -> float:
        """Borne sup (lâche) du score d'une séquence : valeur max + bonus de kill atteignables."""
        kills = sum(1 for e in self.w.enemies if e.life <= seq.ub)
        return seq.ub + self.p.w_kill * kills + self.self_bonus

    # ---- évaluation d'une séquence -----------------------------------------------------------------

    def evaluate(self, seq: Seq, me: Ent, prefix: list[Action], best: Plan | None) -> Plan | None:
        self.evaluations += 1
        stops = seq.stops
        tp = seq.tp_left
        last_i = len(stops) - 1
        groups: list[list[tuple[int, float, tuple]]] = []  # (coût, valeur, payload) — offensif seulement
        support: list[Skill] = []  # soins, boucliers, buffs (soi / alliés) : 2e passe, case finale connue
        shackles: list[tuple[Skill, int, Ent, float]] = []  # (skill, stop, ennemi, montant)
        in_prefix = {a.skill.key for a in prefix if a.skill is not None}
        for sk in me.skills:
            if not sk.available or sk.cost > tp or sk.key in in_prefix:
                continue
            switch = 1 if sk.is_weapon and sk.key != me.weapon_key else 0
            if sk.kind in ATTACKS:
                opts = []
                for si, st in enumerate(stops):
                    for e in self.w.enemies:
                        if not self.can_hit(st.cell, sk, e.cell):
                            continue
                        v1 = unit_damage(sk, me, e, self.p.poison_discount, self.p.w_nova)
                        if v1 <= 0:
                            continue
                        raw1 = unit_raw(sk, me, e)
                        mult = self.target_w[e.id] * (1.0 + self.p.w_low_life * (1.0 - e.life / e.max_life))
                        if sk.kind == POISON and e.poison_load >= e.life * self.p.poison_cap:
                            mult *= self.p.w_poison_overflow  # déjà chargé : un antidote effacerait tout
                        opts.extend((n * sk.cost + switch, n * v1 * mult, (sk, si, e.id, n, n * raw1))
                                    for n in range(1, sk.uses_cap(tp - switch) + 1))
                if opts:
                    groups.append(opts)
            elif sk.kind in SUPPORT:
                if sk.kind not in STAT_BUFFS or sk.turns > 1:
                    support.append(sk)
            elif sk.kind == SUMMON:
                opts = self.summon_options(sk, stops)
                if opts:
                    groups.append(opts)
            elif sk.kind in (SHACKLE_MP, SHACKLE_TP):
                for si, st in enumerate(stops):
                    for e in self.w.enemies:
                        if self.can_hit(st.cell, sk, e.cell):
                            shackles.append((sk, si, e, formulas.shackle(sk.avg, me.magic, me.power)))
                            break
        variants: list[tuple[Skill, int, Ent, float] | None] = [None, *shackles]
        alive_base: dict[int, tuple[str, float]] = {e.id: (BASE, 0.0) for e in self.w.enemies}
        for var in variants:
            tp_v = tp
            forced: list[tuple] = []
            alive0: dict[int, tuple[str, float]] = {e.id: (BASE, 0.0) for e in self.w.enemies}
            if var is not None:
                sk, si, e, amount = var
                tp_v -= sk.cost
                forced.append((sk, si, e.id, 1, 0.0))
                alive0[e.id] = (SHACKLED_MP if sk.kind == SHACKLE_MP else SHACKLED_TP, amount)
            # Passe 1 : sans boucliers → repli → danger final. Passe 2 (si utile) : boucliers valorisés contre
            # ce danger, sac à dos relancé (ils peuvent déplacer des PT pris aux attaques).
            ks = Knapsack(tp_v)
            ks.add(groups)
            value, chosen, kills, alive, end = self.allocate(seq, me, ks, forced, tp_v, alive0)
            if support:
                extra = self.support_groups(support, me, seq, end[0], alive)
                if extra:
                    ks.add(extra)
                    value, chosen, kills, alive, end = self.allocate(seq, me, ks, forced, tp_v, alive0,
                                                                    prev=(kills, end))
            # Létal : dégâts attendus après mes protections vs mes PV. Si des protections peuvent l'éviter,
            # passe de survie : boucliers et soins prennent un crédit `w_death` proportionnel à ce qu'ils
            # absorbent — ils peuvent alors déplacer des PT pris aux attaques.
            lethal, survival = self.lethal_check(chosen, end[1], me)
            if lethal and any(sk.kind not in STAT_BUFFS for sk in support):  # boucliers, soins, +PV max
                ks2 = Knapsack(tp_v)
                ks2.add(groups)
                ks2.add(self.support_groups(support, me, seq, end[0], alive, death_credit=self.p.w_death))
                v2, c2, k2, a2, e2 = self.allocate(seq, me, ks2, forced, tp_v, alive0, prev=(kills, end))
                l2, s2 = self.lethal_check(c2, e2[1], me)
                score1 = value - self.p.w_death
                score2 = v2 - (self.p.w_death if l2 else 0.0)
                if score2 > score1:
                    value, chosen, kills, alive, end, lethal, survival = v2, c2, k2, a2, e2, l2, s2
            end_cell, end_danger, end_pressure, end_via = end
            score = value - self.p.w_safety * end_danger + self.p.w_pressure * end_pressure
            for k in kills:
                score += self.p.w_kill + self.threat(self.d.enemies[k]) * self.future_turns(2)
            if self.w.allies and (var is not None or kills):
                # Entrave ou kill : le danger retiré aux alliés au prochain tour compte comme le mien.
                score += self.p.w_safety * self.ally_relief(alive_base, alive)
            if lethal:
                score -= self.p.w_death
            if seq.teleport_used or end_via == VIA_TELEPORT:
                score -= self.p.w_tp_reserve
            if best is not None and score <= best.score:
                continue
            actions = self.build_actions(seq, prefix, chosen, end_cell, end_via, me)
            best = Plan(actions, score, value, end_danger, end_cell, kills,
                        note=f"pression={end_pressure:.0f} stops={[s.cell for s in stops]} "
                             f"var={var[0].key if var else '-'}",
                        seq=seq, me=me, prefix=prefix, chosen=chosen, alive=alive, end_via=end_via,
                        pressure=end_pressure, lethal=lethal, rel_mult=survival[0], abs_block=survival[1],
                        life_after=survival[2])
            self.finalists.append(best)
        return best

    def support_groups(self, support: list[Skill], me: Ent, seq: Seq, end_cell: int,
                       alive: dict[int, tuple[str, float]], death_credit: float = 0.0
                       ) -> list[list[tuple[int, float, tuple]]]:
        """Un groupe par skill de support, options = sur moi (depuis le dernier arrêt) et sur chaque allié
        à portée d'un arrêt. Case finale connue → l'exposition est réelle.

        Boucliers sur moi : contre l'exposition (danger réel, ou alpha ennemi × probabilité d'engagement :
        on se protège AVANT le one-shot), à rendement décroissant — le k-ième ne réduit que ce qui reste
        après les k−1 meilleurs, × `w_stack^(k−1)` pour en garder pour plus tard. Soins : PV rendus. Buffs de
        stat : engagement × gain d'alpha × tours futurs décotés. Sur un allié : mêmes valeurs avec son
        exposition / ses PV / son alpha, × `w_ally` × son poids, + crédit létal s'il peut mourir.
        `death_credit` (passe de survie) : crédit par PV absorbé sur moi."""
        stops = seq.stops
        last_i = len(stops) - 1
        p = self.p
        options: dict[str, list[tuple[int, float, tuple]]] = {sk.key: [] for sk in support}

        # --- sur moi
        exposure = self.d.exposure(end_cell, alive)
        credit = 1.0 + death_credit / max(1.0, exposure)
        shields = sorted((sk for sk in support if sk.kind in (ABS_SHIELD, REL_SHIELD) and sk.can_target(me, True)),
                         key=lambda s: s.key)
        if shields and exposure > 0:
            rated = [(sk, self.shield_value(sk, me)) for sk in shields]
            rated.sort(key=lambda r: self.absorbed(r[0], r[1], exposure), reverse=True)
            residual = exposure
            stack = 1.0
            for sk, raw in rated:
                v = self.absorbed(sk, raw, residual)
                residual -= v
                v *= stack * (1.0 + self.future_turns(max(0, sk.turns - 1)) * 0.5) * credit
                stack *= p.w_stack
                if v > 0:
                    options[sk.key].append((sk.cost, v, (sk, last_i, me.id, 1, 0.0)))
        engage_me: float | None = None
        for sk in support:
            if not sk.can_target(me, True) or sk.kind in (ABS_SHIELD, REL_SHIELD):
                continue
            if sk.kind == HEAL:
                v = min(self.heal_value(sk, me), me.max_life - me.life) * credit
            elif sk.kind == BOOST_LIFE:
                v = self.heal_value(sk, me) * p.w_boost_life * credit  # +PV et +PV max : jamais plafonné
            else:
                if engage_me is None:
                    engage_me = self.d.engage(end_cell, alive)
                v = engage_me * self.stat_gain(sk, me) * self.future_turns(sk.turns - 1)
            if v > 0:
                options[sk.key].append((sk.cost, v, (sk, last_i, me.id, 1, 0.0)))

        # --- sur les alliés
        for a in self.w.allies:
            wa = p.w_ally * p.ally_weights.get(a.name, 1.0)
            if wa <= 0:
                continue
            reachable = [sk for sk in support if sk.can_target(a, False)]
            if not reachable:
                continue
            exp_a: float | None = None
            for sk in reachable:
                si = next((i for i, st in enumerate(stops) if self.can_hit(st.cell, sk, a.cell)), None)
                if si is None:
                    continue
                if sk.kind in (HEAL, BOOST_LIFE):
                    if sk.kind == HEAL:
                        v = min(self.heal_value(sk, me), a.max_life - a.life)
                    else:
                        v = self.heal_value(sk, me) * p.w_boost_life
                    if exp_a is None:
                        exp_a = self.d.danger_vs(a, a.cell, alive)
                    if exp_a >= a.life * p.lethal_margin:
                        v *= 1.0 + p.w_death / max(1.0, exp_a)
                elif sk.kind in (ABS_SHIELD, REL_SHIELD):
                    if exp_a is None:
                        exp_a = self.d.danger_vs(a, a.cell, alive)
                    v = self.absorbed(sk, self.shield_value(sk, me), exp_a)
                    v *= 1.0 + self.future_turns(max(0, sk.turns - 1)) * 0.5
                    if exp_a >= a.life * p.lethal_margin:
                        v *= 1.0 + p.w_death / max(1.0, exp_a)
                else:
                    v = self.d.engage(a.cell, alive) * self.stat_gain(sk, a) * self.future_turns(sk.turns)
                v *= wa
                if v > 0:
                    options[sk.key].append((sk.cost, v, (sk, si, a.id, 1, 0.0)))
        return [opts for opts in options.values() if opts]

    @staticmethod
    def absorbed(sk: Skill, raw: float, dmg: float) -> float:
        """Part de `dmg` absorbée par un bouclier : absolu par coup (× HITS_PER_TURN), relatif en %."""
        return min(dmg, HITS_PER_TURN * raw) if sk.kind == ABS_SHIELD else dmg * min(100.0, raw) / 100.0

    def allocate(self, seq: Seq, me: Ent, ks: "Knapsack", forced: list[tuple],
                 tp_v: int, alive0: dict[int, tuple[str, float]],
                 prev: tuple[list[int], tuple[int, float, float, str]] | None = None) -> tuple[
                     float, list[tuple], list[int], dict[int, tuple[str, float]], tuple[int, float, float, str]]:
        """Résout le sac à dos courant, puis kills et repli : (valeur, choix, kills, ennemis restants, fin)."""
        value, chosen = ks.solve()
        chosen = chosen + forced
        # Kills : PV réellement retirés ce tour par cible (`raw` : dégâts, part immédiate du nova ; poison 0).
        dealt: dict[int, float] = {}
        for _sk, _si, tid, _n, raw in chosen:
            if raw > 0:
                dealt[tid] = dealt.get(tid, 0.0) + raw
        kills = [e.id for e in self.w.enemies if dealt.get(e.id, 0.0) >= e.life]
        alive = dict(alive0)
        for k in kills:
            alive.pop(k, None)
        tp_after = tp_v - self.spent(chosen, me)
        if prev is not None and prev[0] == kills and (prev[1][3] != VIA_TELEPORT or
                                                      (self.tp_skill and tp_after >= self.tp_skill.cost)):
            end = prev[1]  # les skills sur soi ne déplacent pas : même repli qu'en passe 1
        else:
            end = self.retreat(seq, alive, tp_after)
        return value, chosen, kills, alive, end

    def lethal_check(self, chosen: list[tuple], danger: float, me: Ent) -> tuple[bool, tuple[float, float, float]]:
        """(létal ?, (Π(1 − relatifs), absolus × coups, PV après soins)) pour les protections choisies."""
        rel_mult = 1.0
        abs_block = 0.0
        life = float(me.life)
        for sk, _si, _tid, _n, _raw in chosen:
            if sk.kind == REL_SHIELD:
                rel_mult *= 1.0 - min(100.0, self.shield_value(sk, me)) / 100.0
            elif sk.kind == ABS_SHIELD:
                abs_block += HITS_PER_TURN * self.shield_value(sk, me)
            elif sk.kind == HEAL:
                life = min(float(me.max_life), life + self.heal_value(sk, me))
            elif sk.kind == BOOST_LIFE:
                life += self.heal_value(sk, me)
        incoming = max(0.0, danger * rel_mult - abs_block)
        return incoming >= life * self.p.lethal_margin, (rel_mult, abs_block, life)

    @staticmethod
    def heal_value(sk: Skill, me: Ent) -> float:
        return sk.avg if sk.raw else formulas.heal(sk.avg, me.wisdom, me.power)

    @staticmethod
    def shield_value(sk: Skill, me: Ent) -> float:
        return sk.avg if sk.raw else formulas.shield(sk.avg, me.resistance, me.power)

    @property
    def self_bonus(self) -> float:
        """Valeur max des skills sur soi (soin, boucliers) — pour la borne sup."""
        cached = self.__dict__.get("_self_bonus")
        if cached is not None:
            return cached
        me = self.w.me
        b = 0.0
        for sk in me.skills:
            if not sk.available:
                continue
            if sk.kind == HEAL:
                b += max([min(self.heal_value(sk, me), me.max_life - me.life)]
                         + [min(self.heal_value(sk, me), a.max_life - a.life) for a in self.w.allies])
            elif sk.kind == BOOST_LIFE:
                b += self.heal_value(sk, me) * self.p.w_boost_life
            elif sk.kind in (ABS_SHIELD, REL_SHIELD):
                b += HITS_PER_TURN * self.shield_value(sk, me)
            elif sk.kind in STAT_BUFFS:
                b += self.stat_gain(sk, me) * self.future_turns(sk.turns)
        b *= max([self.p.w_ally, 1.0] + list(self.p.ally_weights.values()))
        self.__dict__["_self_bonus"] = b
        return b

    def spent(self, chosen: list[tuple], me: Ent) -> int:
        tp = 0
        switched: set[str] = set()
        for sk, _si, _tid, n, _raw in chosen:
            tp += n * sk.cost
            if sk.is_weapon and sk.key != me.weapon_key and sk.key not in switched:
                switched.add(sk.key)
                tp += 1
        return tp

    def objective_field(self, alive: dict[int, tuple[str, float]]) -> list[float]:
        """`w_safety × danger − w_pressure × pression − w_cover × couverture` par case, caché par état ennemi
        (une passe sur la grille par état distinct ; la boucle de repli devient un simple lookup)."""
        key = tuple(sorted((eid, v) for eid, (v, _a) in alive.items()))
        obj = self._objective.get(key)
        if obj is None:
            field = self.d.combined(alive)
            pfield = self.d.pressure_field()
            cfield = self.grid.cover_field()
            ws, wp, wc = self.p.w_safety, self.p.w_pressure, self.p.w_cover
            obj = [ws * field[c] - wp * pfield[c] - wc * cfield[c] for c in range(self.grid.n)]
            self._objective[key] = obj
        return obj

    def retreat(self, seq: Seq, alive: dict[int, tuple[str, float]], tp_left: int) -> tuple[int, float, float, str]:
        """Case finale : minimise `w_safety × danger − w_pressure × pression` parmi les cases à pied (PM
        restants). Renvoie (case, danger, pression, via)."""
        last = seq.last
        d = self.d
        field = d.combined(alive)
        obj = self.objective_field(alive)
        best_c, best_o = last, obj[last]
        if seq.mp_left > 0:
            # Tie-break : en zone dangereuse, le plus loin possible de lui ; en zone sûre, au bord de sa zone
            # (on ne fuit pas un ennemi qui ne peut pas nous atteindre : on se rapproche du contact).
            depth = d.depth()
            mp_left = seq.mp_left
            best_t = depth[best_c] if field[best_c] > 0 else -depth[best_c]
            for c, cost in self.reach_from(last, mp_left).items():
                if cost > mp_left:
                    continue
                o = obj[c]
                if o < best_o:
                    best_c, best_o = c, o
                    best_t = depth[c] if field[c] > 0 else -depth[c]
                elif o == best_o:
                    t = depth[c] if field[c] > 0 else -depth[c]
                    if t > best_t:
                        best_c, best_t = c, t
        via = WALK
        if self.tp_skill and not seq.teleport_used and tp_left >= self.tp_skill.cost and field[best_c] > 0:
            tp_c, tp_o = self.best_teleport(last, alive, obj)
            if best_o - tp_o > self.p.w_tp_reserve:
                best_c, best_o, via = tp_c, tp_o, VIA_TELEPORT
        return best_c, field[best_c], d.pressure_field()[best_c], via

    def best_teleport(self, frm: int, alive: dict[int, tuple[str, float]], obj: list[float]) -> tuple[int, float]:
        """Meilleure case de repli par téléportation depuis `frm` (anneau de ~300 cases : caché par état)."""
        key = (frm, tuple(sorted(alive)))
        cache = self.__dict__.setdefault("_best_tp", {})
        r = cache.get(key)
        if r is None:
            best_c, best_o = frm, obj[frm]
            for c in self.teleport_targets(frm):
                o = obj[c]
                if o < best_o:
                    best_c, best_o = c, o
            r = (best_c, best_o)
            cache[key] = r
        return r

    def build_actions(self, seq: Seq, prefix: list[Action], chosen: list[tuple], end_cell: int, end_via: str,
                      me: Ent) -> list[Action]:
        actions = list(prefix)
        by_stop: dict[int, list[tuple]] = {}
        for opt in chosen:
            by_stop.setdefault(opt[1], []).append(opt)
        order = {SUMMON: -2, SHACKLE_MP: 0, SHACKLE_TP: 0, DAMAGE: 1, POISON: 2, HEAL: 3, BOOST_LIFE: 3,
                 ABS_SHIELD: 4, REL_SHIELD: 4}
        order.update(dict.fromkeys(STAT_BUFFS, -1))
        for si, st in enumerate(seq.stops):
            if st.via == WALK:
                actions.append(Action("move", cell=st.cell))
            elif st.via == VIA_TELEPORT:
                actions.append(Action("teleport", cell=st.cell, skill=self.tp_skill))
            for sk, _si, tid, n, _raw in sorted(by_stop.get(si, []), key=lambda o: order.get(o[0].kind, 9)):
                if sk.kind == SUMMON:
                    actions.append(Action("summon", cell=tid, skill=sk))
                else:
                    actions.append(Action("weapon" if sk.is_weapon else "chip", skill=sk, target=tid, n=n))
        if end_cell != seq.last:
            if end_via == VIA_TELEPORT:
                actions.append(Action("teleport", cell=end_cell, skill=self.tp_skill))
            else:
                actions.append(Action("move", cell=end_cell))
        return actions


class Knapsack:
    """Sac à dos « une option par groupe » incrémental : on peut ajouter des groupes après coup (boucliers
    valorisés une fois la case finale connue) sans recalculer les groupes précédents."""

    def __init__(self, tp: int) -> None:
        self.tp = max(0, tp)
        self.best = [0.0] * (self.tp + 1)
        self.back: list[list[tuple | None]] = []

    def add(self, groups: list[list[tuple[int, float, tuple]]]) -> None:
        tp = self.tp
        best = self.best
        for opts in groups:
            new = list(best)
            ch: list[tuple | None] = [None] * (tp + 1)
            for cost, value, payload in opts:
                if cost > tp:
                    continue
                for t in range(cost, tp + 1):
                    v = best[t - cost] + value
                    if v > new[t]:
                        new[t] = v
                        ch[t] = (cost, payload)
            best = new
            self.back.append(ch)
        self.best = best

    def solve(self) -> tuple[float, list[tuple]]:
        """(valeur, payloads choisis)."""
        best = self.best
        t = max(range(self.tp + 1), key=lambda i: best[i])
        total = best[t]
        chosen: list[tuple] = []
        for ch in reversed(self.back):
            c = ch[t]
            if c is not None:
                chosen.append(c[1])
                t -= c[0]
        return total, chosen

    def copy(self) -> "Knapsack":
        k = Knapsack(self.tp)
        k.best = self.best
        k.back = list(self.back)
        return k


def group_knapsack(groups: list[list[tuple[int, float, tuple]]], tp: int) -> tuple[float, list[tuple]]:
    """Sac à dos « une option par groupe » : maximise la valeur sous `tp`. Renvoie (valeur, payloads choisis)."""
    if tp < 0:
        return 0.0, []
    k = Knapsack(tp)
    k.add(groups)
    return k.solve()
