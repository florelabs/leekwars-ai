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
from danger import BASE, SHACKLED_MP, SHACKLED_TP, Danger, unit_damage
from geometry import Grid, bfs_walk
from skills import (
    ABS_SHIELD,
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
    TELEPORT,
    Skill,
)
from tuning import Profile
from world import Ent, World

WALK = "walk"
STAY = "stay"
VIA_TELEPORT = "teleport"
HITS_PER_TURN = 3  # nombre d'attaques ennemies estimé pour valoriser un bouclier absolu


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

    def describe(self) -> str:
        acts = " ; ".join(str(a) for a in self.actions)
        return f"score={self.score:.0f} value={self.value:.0f} danger={self.danger:.0f} kills={self.kills} [{acts}] {self.note}"


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
        self.p = profile
        self.d = danger or Danger(world, profile.poison_discount)
        self.grid: Grid = world.grid
        self._reach: dict[tuple[int, int], dict[int, int]] = {}
        self._objective: dict[tuple, list[float]] = {}
        self._hit: dict[tuple[int, str, int], bool] = {}
        self._tp_ring: dict[int, list[int]] = {}
        self.evaluations = 0
        self.finalists: list[Plan] = []
        self.tp_skill: Skill | None = None
        for s in world.me.skills:
            if s.kind == TELEPORT and s.available and s.cost <= world.me.tp and not world.teleport_used:
                self.tp_skill = s

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
            if sk.kind not in SELF_CONTEXT or not sk.available or sk.cost > me.tp:
                continue
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

    def stat_gain(self, sk: Skill, me: Ent) -> float:
        """Gain d'alpha (dégâts max par tour) apporté par un buff de caractéristique, caché par (skill, me)."""
        key = (sk.key, me.strength, me.magic, me.agility, me.power)
        cache = self.__dict__.setdefault("_gain", {})
        g = cache.get(key)
        if g is None:
            stat = STAT_OF_KIND[sk.kind]
            amount = sk.avg if sk.raw else formulas.buff(sk.avg, me.science, me.power)
            me2 = replace(me, **{stat: getattr(me, stat) + int(amount)})
            g = max(0.0, self.d.my_alpha(me2) - self.d.my_alpha(me))
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
        offensive = [s for s in me.skills if s.kind in (DAMAGE, POISON, SHACKLE_MP, SHACKLE_TP)
                     and s.available and s.cost <= me.tp]
        tp_ring = set(self.teleport_targets(me.cell))
        blocked = self.w.blocked
        ub: dict[int, float] = {}
        per_enemy: list[tuple[list[int], list[int]]] = []
        for e in self.w.enemies:
            walk_cands: dict[int, float] = {}
            tp_cands: dict[int, float] = {}
            for sk in offensive:
                if sk.kind in (DAMAGE, POISON):
                    v = unit_damage(sk, me, e, self.p.poison_discount) * sk.uses_cap(me.tp)
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
                ranked = sorted(cands, key=lambda c: cands[c] - self.p.w_safety * self.d.at(c), reverse=True)
                kept: list[int] = []
                for c in ranked:
                    if any(self.can_hit(c, sk, e.cell) for sk in offensive):
                        kept.append(c)
                        ub[c] = ub.get(c, 0.0) + cands[c]
                        if len(kept) >= k:
                            break
                per_enemy.append((kept, []))
        cells = {me.cell}
        for kept, _ in per_enemy:
            cells.update(kept)
        ub.setdefault(me.cell, 0.0)
        ordered = sorted(cells, key=lambda c: ub[c] - self.p.w_safety * self.d.at(c), reverse=True)
        return ordered, ub

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
            plan.danger = best_danger
            plan.pressure = pfield[best_c]
            plan.end_cell = best_c
            plan.actions = self.build_actions(seq, plan.prefix, plan.chosen, best_c, WALK, plan.me)
            plan.note += f" LOS→{best_danger:.0f}"

    def plan_ctx(self, me: Ent, prefix: list[Action]) -> Plan | None:
        reach0 = self.reach_from(me.cell, me.mp)
        cells, ub = self.waypoints(me, reach0)
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
        groups: list[list[tuple[int, float, tuple]]] = []  # (coût, valeur, payload) — hors boucliers
        shields: list[Skill] = []  # valorisés contre le danger de la case FINALE, connue après le repli
        buffs: list[Skill] = []  # buffs de stat (hors celui du contexte) : valeur = engagement × gain futur
        shackles: list[tuple[Skill, int, Ent, float]] = []  # (skill, stop, ennemi, montant)
        in_prefix = {a.skill.key for a in prefix if a.skill is not None}
        for sk in me.skills:
            if not sk.available or sk.cost > tp or sk.key in in_prefix:
                continue
            switch = 1 if sk.is_weapon and sk.key != me.weapon_key else 0
            if sk.kind in (DAMAGE, POISON):
                opts = []
                for si, st in enumerate(stops):
                    for e in self.w.enemies:
                        if not self.can_hit(st.cell, sk, e.cell):
                            continue
                        v1 = unit_damage(sk, me, e, self.p.poison_discount)
                        if v1 <= 0:
                            continue
                        mult = 1.0 + self.p.w_low_life * (1.0 - e.life / e.max_life)
                        opts.extend((n * sk.cost + switch, n * v1 * mult, (sk, si, e.id, n, n * v1))
                                    for n in range(1, sk.uses_cap(tp - switch) + 1))
                if opts:
                    groups.append(opts)
            elif sk.kind == HEAL:
                v = min(self.heal_value(sk, me), me.max_life - me.life)
                if v > 0:
                    groups.append([(sk.cost, v, (sk, last_i, me.id, 1, 0.0))])
            elif sk.kind in (ABS_SHIELD, REL_SHIELD):
                shields.append(sk)
            elif sk.kind in STAT_BUFFS and sk.turns > 1:
                buffs.append(sk)
            elif sk.kind in (SHACKLE_MP, SHACKLE_TP):
                for si, st in enumerate(stops):
                    for e in self.w.enemies:
                        if self.can_hit(st.cell, sk, e.cell):
                            shackles.append((sk, si, e, formulas.shackle(sk.avg, me.magic, me.power)))
                            break
        variants: list[tuple[Skill, int, Ent, float] | None] = [None, *shackles]
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
            if shields or buffs:
                extra = self.self_groups(shields, buffs, me, end[0], alive, last_i)
                if extra:
                    ks.add(extra)
                    value, chosen, kills, alive, end = self.allocate(seq, me, ks, forced, tp_v, alive0,
                                                                    prev=(kills, end))
            end_cell, end_danger, end_pressure, end_via = end
            score = value - self.p.w_safety * end_danger + self.p.w_pressure * end_pressure \
                + self.p.w_kill * len(kills)
            if seq.teleport_used or end_via == VIA_TELEPORT:
                score -= self.p.w_tp_reserve
            if best is not None and score <= best.score:
                continue
            actions = self.build_actions(seq, prefix, chosen, end_cell, end_via, me)
            best = Plan(actions, score, value, end_danger, end_cell, kills,
                        note=f"pression={end_pressure:.0f} stops={[s.cell for s in stops]} "
                             f"var={var[0].key if var else '-'}",
                        seq=seq, me=me, prefix=prefix, chosen=chosen, alive=alive, end_via=end_via,
                        pressure=end_pressure)
            self.finalists.append(best)
        return best

    def self_groups(self, shields: list[Skill], buffs: list[Skill], me: Ent, end_cell: int,
                    alive: dict[int, tuple[str, float]], stop: int) -> list[list[tuple[int, float, tuple]]]:
        """Groupes de sac à dos pour les skills sur soi, une fois la case finale connue.

        Boucliers : valorisés contre l'exposition (danger réel, ou alpha ennemi × probabilité d'engagement :
        on se protège AVANT le one-shot), à rendement décroissant — le k-ième bouclier ne réduit que ce qui
        reste après les k−1 meilleurs, et prend un facteur `w_stack^(k−1)` pour en garder pour plus tard
        (cooldowns désynchronisés). Buffs de stat : engagement × gain d'alpha × tours futurs décotés."""
        groups: list[list[tuple[int, float, tuple]]] = []
        exposure = self.d.exposure(end_cell, alive) if (shields or buffs) else 0.0
        if shields and exposure > 0:
            def absorbed(sk: Skill, raw: float, dmg: float) -> float:
                """Part de `dmg` absorbée par le bouclier : absolu par coup (×HITS_PER_TURN), relatif en %."""
                return min(dmg, HITS_PER_TURN * raw) if sk.kind == ABS_SHIELD else dmg * min(100.0, raw) / 100.0

            rated = [(sk, self.shield_value(sk, me)) for sk in shields]
            rated.sort(key=lambda r: absorbed(r[0], r[1], exposure), reverse=True)
            residual = exposure
            stack = 1.0
            for sk, raw in rated:
                v = absorbed(sk, raw, residual)
                residual -= v
                v *= stack * (1.0 + self.future_turns(max(0, sk.turns - 1)) * 0.5)
                stack *= self.p.w_stack
                if v > 0:
                    groups.append([(sk.cost, v, (sk, stop, me.id, 1, 0.0))])
        if buffs:
            engage = self.d.engage(end_cell, alive)
            if engage > 0:
                for sk in buffs:
                    v = engage * self.stat_gain(sk, me) * self.future_turns(sk.turns - 1)
                    if v > 0:
                        groups.append([(sk.cost, v, (sk, stop, me.id, 1, 0.0))])
        return groups

    def allocate(self, seq: Seq, me: Ent, ks: "Knapsack", forced: list[tuple],
                 tp_v: int, alive0: dict[int, tuple[str, float]],
                 prev: tuple[list[int], tuple[int, float, float, str]] | None = None) -> tuple[
                     float, list[tuple], list[int], dict[int, tuple[str, float]], tuple[int, float, float, str]]:
        """Résout le sac à dos courant, puis kills et repli : (valeur, choix, kills, ennemis restants, fin)."""
        value, chosen = ks.solve()
        chosen = chosen + forced
        # Kills : dégâts bruts cumulés par cible (les poisons ne tuent pas ce tour : on ne compte que DAMAGE).
        dealt: dict[int, float] = {}
        for sk, _si, tid, _n, raw in chosen:
            if sk.kind == DAMAGE:
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

    @staticmethod
    def heal_value(sk: Skill, me: Ent) -> float:
        return sk.avg if sk.raw else formulas.heal(sk.avg, me.wisdom, me.power)

    @staticmethod
    def shield_value(sk: Skill, me: Ent) -> float:
        return sk.avg if sk.raw else formulas.shield(sk.avg, me.resistance, me.power)

    @property
    def self_bonus(self) -> float:
        """Valeur max des skills sur soi (soin, boucliers) — pour la borne sup."""
        me = self.w.me
        b = 0.0
        for sk in me.skills:
            if sk.kind == HEAL and sk.available:
                b += min(self.heal_value(sk, me), me.max_life - me.life)
            elif sk.kind in (ABS_SHIELD, REL_SHIELD) and sk.available:
                b += HITS_PER_TURN * self.shield_value(sk, me)
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
        order = {SHACKLE_MP: 0, SHACKLE_TP: 0, DAMAGE: 1, POISON: 2, HEAL: 3, ABS_SHIELD: 4, REL_SHIELD: 4}
        order.update(dict.fromkeys(STAT_BUFFS, -1))
        for si, st in enumerate(seq.stops):
            if st.via == WALK:
                actions.append(Action("move", cell=st.cell))
            elif st.via == VIA_TELEPORT:
                actions.append(Action("teleport", cell=st.cell, skill=self.tp_skill))
            for sk, _si, tid, n, _raw in sorted(by_stop.get(si, []), key=lambda o: order.get(o[0].kind, 9)):
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
