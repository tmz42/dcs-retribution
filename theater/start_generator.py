from __future__ import annotations

import logging
import math
import pickle
import random
from typing import Any, Dict, List, Optional

from dcs.mapping import Point
from dcs.task import CAP, CAS, PinpointStrike
from dcs.vehicles import AirDefence

from game import Game, db
from game.factions.faction import Faction
from game.settings import Settings
from game.version import VERSION
from gen import namegen
from gen.defenses.armor_group_generator import generate_armor_group
from gen.fleet.ship_group_generator import (
    generate_carrier_group,
    generate_lha_group,
    generate_ship_group,
)
from gen.missiles.missiles_group_generator import generate_missile_group
from gen.sam.sam_group_generator import (
    generate_anti_air_group,
    generate_shorad_group,
)
from theater import (
    ConflictTheater,
    ControlPoint,
    ControlPointType,
    TheaterGroundObject,
)
from theater.conflicttheater import IMPORTANCE_HIGH, IMPORTANCE_LOW
from theater.theatergroundobject import (
    SamGroundObject, BuildingGroundObject, CarrierGroundObject,
    LhaGroundObject,
    MissileSiteGroundObject, ShipGroundObject,
)

GroundObjectTemplates = Dict[str, Dict[str, Any]]

UNIT_VARIETY = 6
UNIT_AMOUNT_FACTOR = 16
UNIT_COUNT_IMPORTANCE_LOG = 1.3

COUNT_BY_TASK = {
    PinpointStrike: 12,
    CAP: 8,
    CAS: 4,
    AirDefence: 1,
}


class GameGenerator:
    def __init__(self, player: str, enemy: str, theater: ConflictTheater,
                 settings: Settings, start_date, starting_budget: int,
                 multiplier: float, midgame: bool) -> None:
        self.player = player
        self.enemy = enemy
        self.theater = theater
        self.settings = settings
        self.start_date = start_date
        self.starting_budget = starting_budget
        self.multiplier = multiplier
        self.midgame = midgame

    def generate(self) -> Game:
        # Reset name generator
        namegen.reset()
        self.prepare_theater()
        self.populate_red_airbases()

        game = Game(player_name=self.player,
                    enemy_name=self.enemy,
                    theater=self.theater,
                    start_date=self.start_date,
                    settings=self.settings)

        GroundObjectGenerator(game).generate()
        game.budget = self.starting_budget
        game.settings.multiplier = self.multiplier
        game.settings.sams = True
        game.settings.version = VERSION
        return game

    def prepare_theater(self) -> None:
        to_remove = []

        # Auto-capture half the bases if midgame.
        if self.midgame:
            control_points = self.theater.controlpoints
            for control_point in control_points[:len(control_points) // 2]:
                control_point.captured = True

        # Remove carrier and lha, invert situation if needed
        for cp in self.theater.controlpoints:
            no_carrier = self.settings.do_not_generate_carrier
            no_lha = self.settings.do_not_generate_lha
            if cp.cptype is ControlPointType.AIRCRAFT_CARRIER_GROUP and \
                    no_carrier:
                to_remove.append(cp)
            elif cp.cptype is ControlPointType.LHA_GROUP and no_lha:
                to_remove.append(cp)

            if self.settings.inverted:
                cp.captured = cp.captured_invert

        # do remove
        for cp in to_remove:
            self.theater.controlpoints.remove(cp)

        # TODO: Fix this. This captures all bases for blue.
        # reapply midgame inverted if needed
        if self.midgame and self.settings.inverted:
            for i, cp in enumerate(reversed(self.theater.controlpoints)):
                if i > len(self.theater.controlpoints):
                    break
                else:
                    cp.captured = True

    def populate_red_airbases(self) -> None:
        for control_point in self.theater.enemy_points():
            if control_point.captured:
                continue
            self.populate_red_airbase(control_point)

    def populate_red_airbase(self, control_point: ControlPoint) -> None:
        # Force reset cp on generation
        control_point.base.aircraft = {}
        control_point.base.armor = {}
        control_point.base.aa = {}
        control_point.base.commision_points = {}
        control_point.base.strength = 1

        for task in [PinpointStrike, CAP, CAS, AirDefence]:
            if IMPORTANCE_HIGH <= control_point.importance <= IMPORTANCE_LOW:
                raise ValueError(
                    f"CP importance must be between {IMPORTANCE_LOW} and "
                    f"{IMPORTANCE_HIGH}, is {control_point.importance}")

            importance_factor = ((control_point.importance - IMPORTANCE_LOW) /
                                 (IMPORTANCE_HIGH - IMPORTANCE_LOW))
            # noinspection PyTypeChecker
            unit_types = db.choose_units(task, importance_factor, UNIT_VARIETY,
                                         self.enemy)
            if not unit_types:
                continue

            count_log = math.log(control_point.importance + 0.01,
                                 UNIT_COUNT_IMPORTANCE_LOG)
            count = max(
                COUNT_BY_TASK[task] * self.multiplier * (1 + count_log), 1
            )

            count_per_type = max(int(float(count) / len(unit_types)), 1)
            for unit_type in unit_types:
                control_point.base.commision_units({unit_type: count_per_type})


class ControlPointGroundObjectGenerator:
    def __init__(self, game: Game, control_point: ControlPoint,
                 templates: GroundObjectTemplates) -> None:
        self.game = game
        self.control_point = control_point
        self.templates = templates

    @property
    def faction_name(self) -> str:
        if self.control_point.captured:
            return self.game.player_name
        else:
            return self.game.enemy_name

    @property
    def faction(self) -> Faction:
        return db.FACTIONS[self.faction_name]

    def generate(self) -> bool:
        self.control_point.ground_objects = []
        self.generate_ground_points()
        if self.faction.navy_generators:
            # Even airbases can generate navies if they are close enough to the
            # water. This is not controlled by the control point definition, but
            # rather by whether or not the generator can find a valid position
            # for the ship.
            self.generate_navy()

        if self.faction.missiles:
            # TODO: Presumably only for airbases?
            self.generate_missile_sites()

        return True

    def generate_ground_points(self) -> None:
        """Generate ground objects and AA sites for the control point."""

        if self.control_point.is_global:
            return

        # TODO: Should probably perform this check later.
        # Just because we don't have factories for the faction doesn't mean we
        # shouldn't generate AA.
        available_categories = self.faction.building_set
        if not available_categories:
            return

        # Always generate at least one AA point.
        self.generate_aa_site()

        # And between 2 and 7 other objectives.
        amount = random.randrange(2, 7)
        for i in range(amount):
            # 1 in 4 additional objectives are AA.
            if random.randint(0, 3) == 0:
                self.generate_aa_site()
            else:
                category = random.choice(available_categories)
                self.generate_ground_point(category)

    def generate_ground_point(self, category: str) -> None:
        obj_name = namegen.random_objective_name()
        template = random.choice(list(self.templates[category].values()))
        point = find_location(category != "oil",
                              self.control_point.position,
                              self.game.theater, 10000, 40000,
                              self.control_point.ground_objects)

        if point is None:
            logging.error(
                f"Could not find point for {obj_name} at {self.control_point}")
            return

        object_id = 0
        group_id = self.game.next_group_id()

        # TODO: Create only one TGO per objective, each with multiple units.
        for unit in template:
            object_id += 1

            template_point = Point(unit["offset"].x, unit["offset"].y)
            g = BuildingGroundObject(
                obj_name, category, group_id, object_id, point + template_point,
                unit["heading"], self.control_point, unit["type"])

            self.control_point.ground_objects.append(g)

    def generate_aa_site(self) -> None:
        obj_name = namegen.random_objective_name()
        position = find_location(True, self.control_point.position,
                                 self.game.theater, 10000, 40000,
                                 self.control_point.ground_objects)

        if position is None:
            logging.error(
                f"Could not find point for {obj_name} at {self.control_point}")
            return

        group_id = self.game.next_group_id()

        g = SamGroundObject(namegen.random_objective_name(), group_id,
                            position, self.control_point, for_airbase=False)
        group = generate_anti_air_group(self.game, g, self.faction_name)
        if group is not None:
            g.groups = [group]
        self.control_point.ground_objects.append(g)

    def generate_navy(self) -> None:
        skip_player_navy = self.game.settings.do_not_generate_player_navy
        if self.control_point.captured and skip_player_navy:
            return

        skip_enemy_navy = self.game.settings.do_not_generate_enemy_navy
        if not self.control_point.captured and skip_enemy_navy:
            return

        for _ in range(self.faction.navy_group_count):
            self.generate_ship()

    def generate_ship(self) -> None:
        point = find_location(False, self.control_point.position,
                              self.game.theater, 5000, 40000, [], False)
        if point is None:
            logging.error(
                f"Could not find point for {self.control_point}'s navy")
            return

        group_id = self.game.next_group_id()

        g = ShipGroundObject(namegen.random_objective_name(), group_id, point,
                             self.control_point)

        group = generate_ship_group(self.game, g, self.faction_name)
        g.groups = []
        if group is not None:
            g.groups.append(group)
            self.control_point.ground_objects.append(g)

    def generate_missile_sites(self) -> None:
        for i in range(self.faction.missiles_group_count):
            self.generate_missile_site()

    def generate_missile_site(self) -> None:
        point = find_location(True, self.control_point.position,
                              self.game.theater, 2500, 40000, [], False)
        if point is None:
            logging.info(
                f"Could not find point for {self.control_point} missile site")
            return

        group_id = self.game.next_group_id()

        g = MissileSiteGroundObject(namegen.random_objective_name(), group_id,
                                    point, self.control_point)
        group = generate_missile_group(self.game, g, self.faction_name)
        g.groups = []
        if group is not None:
            g.groups.append(group)
            self.control_point.ground_objects.append(g)
        return


class CarrierGroundObjectGenerator(ControlPointGroundObjectGenerator):
    def generate(self) -> bool:
        if not super().generate():
            return False

        carrier_names = self.faction.carrier_names
        if not carrier_names:
            logging.info(
                f"Skipping generation of {self.control_point.name} because "
                f"{self.faction_name} has no carriers")
            return False

        # Create ground object group
        group_id = self.game.next_group_id()
        g = CarrierGroundObject(namegen.random_objective_name(), group_id,
                                self.control_point)
        group = generate_carrier_group(self.faction_name, self.game, g)
        g.groups = []
        if group is not None:
            g.groups.append(group)
        self.control_point.ground_objects.append(g)
        self.control_point.name = random.choice(carrier_names)
        return True


class LhaGroundObjectGenerator(ControlPointGroundObjectGenerator):
    def generate(self) -> bool:
        if not super().generate():
            return False

        lha_names = self.faction.helicopter_carrier_names
        if not lha_names:
            logging.info(
                f"Skipping generation of {self.control_point.name} because "
                f"{self.faction_name} has no LHAs")
            return False

        # Create ground object group
        group_id = self.game.next_group_id()
        g = LhaGroundObject(namegen.random_objective_name(), group_id,
                            self.control_point)
        group = generate_lha_group(self.faction_name, self.game, g)
        g.groups = []
        if group is not None:
            g.groups.append(group)
        self.control_point.ground_objects.append(g)
        self.control_point.name = random.choice(lha_names)
        return True


class AirbaseGroundObjectGenerator(ControlPointGroundObjectGenerator):
    def generate(self) -> bool:
        if not super().generate():
            return False

        for i in range(random.randint(3, 6)):
            self.generate_sam(i)
        return True

    def generate_sam(self, index: int) -> None:
        position = find_location(True, self.control_point.position,
                                 self.game.theater, 800, 3200, [], True)
        if position is None:
            logging.error("Could not find position for "
                          f"{self.control_point} base defense")
            return

        group_id = self.game.next_group_id()

        g = SamGroundObject(namegen.random_objective_name(), group_id,
                            position, self.control_point, for_airbase=True)

        generate_airbase_defense_group(index, g, self.faction_name, self.game)
        self.control_point.ground_objects.append(g)


class GroundObjectGenerator:
    def __init__(self, game: Game) -> None:
        self.game = game
        with open("resources/groundobject_templates.p", "rb") as f:
            self.templates: GroundObjectTemplates = pickle.load(f)

    def generate(self) -> None:
        # Copied so we can remove items from the original list without breaking
        # the iterator.
        control_points = list(self.game.theater.controlpoints)
        for control_point in control_points:
            if not self.generate_for_control_point(control_point):
                self.game.theater.controlpoints.remove(control_point)

    def generate_for_control_point(self, control_point: ControlPoint) -> bool:
        generator: ControlPointGroundObjectGenerator
        if control_point.cptype == ControlPointType.AIRCRAFT_CARRIER_GROUP:
            generator = CarrierGroundObjectGenerator(self.game, control_point,
                                                     self.templates)
        elif control_point.cptype == ControlPointType.LHA_GROUP:
            generator = LhaGroundObjectGenerator(self.game, control_point,
                                                 self.templates)
        else:
            generator = AirbaseGroundObjectGenerator(self.game, control_point,
                                                     self.templates)
        return generator.generate()


def generate_airbase_defense_group(airbase_defense_group_id: int,
                                   ground_obj: TheaterGroundObject,
                                   faction: str, game: Game) -> None:
    if airbase_defense_group_id == 0:
        group = generate_armor_group(faction, game, ground_obj)
    elif airbase_defense_group_id == 1 and random.randint(0, 1) == 0:
        group = generate_anti_air_group(game, ground_obj, faction)
    elif random.randint(0, 2) == 1:
        group = generate_shorad_group(game, ground_obj, faction)
    else:
        group = generate_armor_group(faction, game, ground_obj)

    ground_obj.groups = []
    if group is not None:
        ground_obj.groups.append(group)


# TODO: https://stackoverflow.com/a/19482012/632035
# A lot of the time spent on mission generation is spent in this function since
# just randomly guess up to 1800 times and often fail. This is particularly
# problematic while trying to find placement for navies in Nevada.
def find_location(on_ground: bool, near: Point, theater: ConflictTheater,
                  min_range: int, max_range: int,
                  others: List[TheaterGroundObject],
                  is_base_defense: bool = False) -> Optional[Point]:
    """
    Find a valid ground object location
    :param on_ground: Whether it should be on ground or on sea (True = on
    ground)
    :param near: Point
    :param theater: Theater object
    :param min_range: Minimal range from point
    :param max_range: Max range from point
    :param others: Other already existing ground objects
    :param is_base_defense: True if the location is for base defense.
    :return:
    """
    point = None
    for _ in range(300):

        # Check if on land or sea
        p = near.random_point_within(max_range, min_range)
        if on_ground and theater.is_on_land(p):
            point = p
        elif not on_ground and theater.is_in_sea(p):
            point = p

        if point:
            for angle in range(0, 360, 45):
                p = point.point_from_heading(angle, 2500)
                if on_ground and not theater.is_on_land(p):
                    point = None
                    break
                elif not on_ground and not theater.is_in_sea(p):
                    point = None
                    break
        if point:
            for other in others:
                if other.position.distance_to_point(point) < 10000:
                    point = None
                    break

        if point:
            for control_point in theater.controlpoints:
                if is_base_defense:
                    break
                if control_point.position != near:
                    if point is None:
                        break
                    if control_point.position.distance_to_point(point) < 30000:
                        point = None
                        break
                    for ground_obj in control_point.ground_objects:
                        if ground_obj.position.distance_to_point(point) < 10000:
                            point = None
                            break

        if point:
            return point
    return None
