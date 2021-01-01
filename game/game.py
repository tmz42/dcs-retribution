import itertools
import logging
import random
import sys
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List

from dcs.action import Coalition
from dcs.mapping import Point
from dcs.task import CAP, CAS, PinpointStrike
from dcs.vehicles import AirDefence

from game import db
from game.inventory import GlobalAircraftInventory
from game.models.game_stats import GameStats
from game.plugins import LuaPluginManager
from gen.ato import AirTaskingOrder
from gen.conflictgen import Conflict
from gen.flights.ai_flight_planner import CoalitionMissionPlanner
from gen.flights.closestairfields import ObjectiveDistanceCache
from gen.flights.flight import FlightType
from gen.ground_forces.ai_ground_planner import GroundPlanner
from . import persistency
from .debriefing import Debriefing
from .event.event import Event, UnitsDeliveryEvent
from .event.frontlineattack import FrontlineAttackEvent
from .factions.faction import Faction
from .income import Income
from .infos.information import Information
from .navmesh import NavMesh
from .procurement import ProcurementAi
from .settings import Settings
from .theater import ConflictTheater, ControlPoint
from .threatzones import ThreatZones
from .unitmap import UnitMap
from .weather import Conditions, TimeOfDay

COMMISION_UNIT_VARIETY = 4
COMMISION_LIMITS_SCALE = 1.5
COMMISION_LIMITS_FACTORS = {
    PinpointStrike: 10,
    CAS: 5,
    CAP: 8,
    AirDefence: 8,
}

COMMISION_AMOUNTS_SCALE = 1.5
COMMISION_AMOUNTS_FACTORS = {
    PinpointStrike: 3,
    CAS: 1,
    CAP: 2,
    AirDefence: 0.8,
}

PLAYER_INTERCEPT_GLOBAL_PROBABILITY_BASE = 30
PLAYER_INTERCEPT_GLOBAL_PROBABILITY_LOG = 2
PLAYER_BASEATTACK_THRESHOLD = 0.4

# amount of strength player bases recover for the turn
PLAYER_BASE_STRENGTH_RECOVERY = 0.2

# amount of strength enemy bases recover for the turn
ENEMY_BASE_STRENGTH_RECOVERY = 0.05

# cost of AWACS for single operation
AWACS_BUDGET_COST = 4

# Bonus multiplier logarithm base
PLAYER_BUDGET_IMPORTANCE_LOG = 2


class TurnState(Enum):
    WIN = 0
    LOSS = 1
    CONTINUE = 2


class Game:
    def __init__(self, player_name: str, enemy_name: str,
                 theater: ConflictTheater, start_date: datetime,
                 settings: Settings, player_budget: float,
                 enemy_budget: float) -> None:
        self.settings = settings
        self.events: List[Event] = []
        self.theater = theater
        self.player_name = player_name
        self.player_country = db.FACTIONS[player_name].country
        self.enemy_name = enemy_name
        self.enemy_country = db.FACTIONS[enemy_name].country
        self.turn = 0
        self.date = date(start_date.year, start_date.month, start_date.day)
        self.game_stats = GameStats()
        self.game_stats.update(self)
        self.ground_planners: Dict[int, GroundPlanner] = {}
        self.informations = []
        self.informations.append(Information("Game Start", "-" * 40, 0))
        self.__culling_points: List[Point] = []
        self.__destroyed_units: List[str] = []
        self.savepath = ""
        self.budget = player_budget
        self.enemy_budget = enemy_budget
        self.current_unit_id = 0
        self.current_group_id = 0

        self.conditions = self.generate_conditions()

        self.blue_ato = AirTaskingOrder()
        self.red_ato = AirTaskingOrder()

        self.aircraft_inventory = GlobalAircraftInventory(
            self.theater.controlpoints
        )

        self.sanitize_sides()

        self.on_load()

        # Turn 0 procurement. We don't actually have any missions to plan, but
        # the planner will tell us what it would like to plan so we can use that
        # to drive purchase decisions.
        blue_planner = CoalitionMissionPlanner(self, is_player=True)
        blue_planner.plan_missions()

        red_planner = CoalitionMissionPlanner(self, is_player=False)
        red_planner.plan_missions()

        self.plan_procurement(blue_planner, red_planner)

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        # Avoid persisting any volatile types that can be deterministically
        # recomputed on load for the sake of save compatibility.
        del state["blue_threat_zone"]
        del state["red_threat_zone"]
        del state["blue_navmesh"]
        del state["red_navmesh"]
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        self.__dict__.update(state)
        # Regenerate any state that was not persisted.
        self.on_load()

    def generate_conditions(self) -> Conditions:
        return Conditions.generate(self.theater, self.date,
                                   self.current_turn_time_of_day, self.settings)

    def sanitize_sides(self):
        """
        Make sure the opposing factions are using different countries
        :return:
        """
        if self.player_country == self.enemy_country:
            if self.player_country == "USA":
                self.enemy_country = "USAF Aggressors"
            elif self.player_country == "Russia":
                self.enemy_country = "USSR"
            else:
                self.enemy_country = "Russia"

    @property
    def player_faction(self) -> Faction:
        return db.FACTIONS[self.player_name]

    @property
    def enemy_faction(self) -> Faction:
        return db.FACTIONS[self.enemy_name]

    def faction_for(self, player: bool) -> Faction:
        if player:
            return self.player_faction
        return self.enemy_faction

    def _roll(self, prob, mult):
        if self.settings.version == "dev":
            # always generate all events for dev
            return 100
        else:
            return random.randint(1, 100) <= prob * mult

    def _generate_player_event(self, event_class, player_cp, enemy_cp):
        self.events.append(event_class(self, player_cp, enemy_cp, enemy_cp.position, self.player_name, self.enemy_name))

    def _generate_events(self):
        for front_line in self.theater.conflicts(True):
            self._generate_player_event(FrontlineAttackEvent,
                                        front_line.control_point_a,
                                        front_line.control_point_b)

    def adjust_budget(self, amount: float, player: bool) -> None:
        if player:
            self.budget += amount
        else:
            self.enemy_budget += amount

    def process_player_income(self):
        self.budget += Income(self, player=True).total

    def process_enemy_income(self):
        # TODO: Clean up save compat.
        if not hasattr(self, "enemy_budget"):
            self.enemy_budget = 0
        self.enemy_budget += Income(self, player=False).total

    def initiate_event(self, event: Event) -> UnitMap:
        #assert event in self.events
        logging.info("Generating {} (regular)".format(event))
        return event.generate()

    def finish_event(self, event: Event, debriefing: Debriefing):
        logging.info("Finishing event {}".format(event))
        event.commit(debriefing)

        if event in self.events:
            self.events.remove(event)
        else:
            logging.info("finish_event: event not in the events!")

    def is_player_attack(self, event):
        if isinstance(event, Event):
            return event and event.attacker_name and event.attacker_name == self.player_name
        else:
            raise RuntimeError(f"{event} was passed when an Event type was expected")

    def on_load(self) -> None:
        LuaPluginManager.load_settings(self.settings)
        ObjectiveDistanceCache.set_theater(self.theater)
        self.compute_conflicts_position()
        self.compute_threat_zones()

    def pass_turn(self, no_action: bool = False) -> None:
        logging.info("Pass turn")
        self.informations.append(Information("End of turn #" + str(self.turn), "-" * 40, 0))
        self.turn += 1

        for control_point in self.theater.controlpoints:
            control_point.process_turn(self)

        self.process_enemy_income()

        self.process_player_income()

        if not no_action and self.turn > 1:
            for cp in self.theater.player_points():
                cp.base.affect_strength(+PLAYER_BASE_STRENGTH_RECOVERY)
        else:
            for cp in self.theater.player_points():
                if not cp.is_carrier and not cp.is_lha:
                    cp.base.affect_strength(-PLAYER_BASE_STRENGTH_RECOVERY)

        self.conditions = self.generate_conditions()

        self.initialize_turn()

        # Autosave progress
        persistency.autosave(self)

    def check_win_loss(self):
        captured_states = {i.captured for i in self.theater.controlpoints}
        if True not in captured_states:
            return TurnState.LOSS
        if False not in captured_states:
            return TurnState.WIN
        return TurnState.CONTINUE

    def initialize_turn(self) -> None:
        self.events = []
        self._generate_events()

        # Update statistics
        self.game_stats.update(self)

        self.aircraft_inventory.reset()
        for cp in self.theater.controlpoints:
            self.aircraft_inventory.set_from_control_point(cp)

        # Check for win or loss condition
        turn_state = self.check_win_loss()
        if turn_state in (TurnState.LOSS,TurnState.WIN):
            return self.process_win_loss(turn_state)

        # Plan flights & combat for next turn
        self.compute_conflicts_position()
        self.compute_threat_zones()
        self.ground_planners = {}
        self.blue_ato.clear()
        self.red_ato.clear()

        blue_planner = CoalitionMissionPlanner(self, is_player=True)
        blue_planner.plan_missions()

        red_planner = CoalitionMissionPlanner(self, is_player=False)
        red_planner.plan_missions()

        for cp in self.theater.controlpoints:
            if cp.has_frontline:
                gplanner = GroundPlanner(cp, self)
                gplanner.plan_groundwar()
                self.ground_planners[cp.id] = gplanner

        self.plan_procurement(blue_planner, red_planner)

    def plan_procurement(self, blue_planner: CoalitionMissionPlanner,
                         red_planner: CoalitionMissionPlanner) -> None:
        # The first turn needs to buy a *lot* of aircraft to fill CAPs, so it
        # gets much more of the budget that turn. Otherwise budget (after
        # repairs) is split evenly between air and ground. For the default
        # starting budget of 2000 this gives 600 to ground forces and 1400 to
        # aircraft.
        ground_portion = 0.3 if self.turn == 0 else 0.5
        self.budget = ProcurementAi(
            self,
            for_player=True,
            faction=self.player_faction,
            manage_runways=self.settings.automate_runway_repair,
            manage_front_line=self.settings.automate_front_line_reinforcements,
            manage_aircraft=self.settings.automate_aircraft_reinforcements,
            front_line_budget_share=ground_portion
        ).spend_budget(self.budget, blue_planner.procurement_requests)

        self.enemy_budget = ProcurementAi(
            self,
            for_player=False,
            faction=self.enemy_faction,
            manage_runways=True,
            manage_front_line=True,
            manage_aircraft=True,
            front_line_budget_share=ground_portion
        ).spend_budget(self.enemy_budget, red_planner.procurement_requests)

    def message(self, text: str) -> None:
        self.informations.append(Information(text, turn=self.turn))

    @property
    def current_turn_time_of_day(self) -> TimeOfDay:
        return list(TimeOfDay)[self.turn % 4]

    @property
    def current_day(self) -> date:
        return self.date + timedelta(days=self.turn // 4)

    def next_unit_id(self):
        """
        Next unit id for pre-generated units
        """
        self.current_unit_id += 1
        return self.current_unit_id

    def next_group_id(self):
        """
        Next unit id for pre-generated units
        """
        self.current_group_id += 1
        return self.current_group_id

    def compute_threat_zones(self) -> None:
        self.blue_threat_zone = ThreatZones.for_faction(self, player=True)
        self.red_threat_zone = ThreatZones.for_faction(self, player=False)
        self.blue_navmesh = NavMesh.from_threat_zones(self.red_threat_zone,
                                                      self.theater)
        self.red_navmesh = NavMesh.from_threat_zones(self.blue_threat_zone,
                                                     self.theater)

    def threat_zone_for(self, player: bool) -> ThreatZones:
        if player:
            return self.blue_threat_zone
        return self.red_threat_zone

    def navmesh_for(self, player: bool) -> NavMesh:
        if player:
            return self.blue_navmesh
        return self.red_navmesh

    def compute_conflicts_position(self):
        """
        Compute the current conflict center position(s), mainly used for culling calculation
        :return: List of points of interests
        """
        points = []

        # By default, use the existing frontline conflict position
        for front_line in self.theater.conflicts():
            position = Conflict.frontline_position(front_line.control_point_a,
                                                   front_line.control_point_b,
                                                   self.theater)
            points.append(position[0])
            points.append(front_line.control_point_a.position)
            points.append(front_line.control_point_b.position)

        # If do_not_cull_carrier is enabled, add carriers as culling point
        if self.settings.perf_do_not_cull_carrier:
            for cp in self.theater.controlpoints:
                if cp.is_carrier or cp.is_lha:
                    points.append(cp.position)

        # If there is no conflict take the center point between the two nearest opposing bases
        if len(points) == 0:
            cpoint = None
            min_distance = sys.maxsize
            for cp in self.theater.player_points():
                for cp2 in self.theater.enemy_points():
                    d = cp.position.distance_to_point(cp2.position)
                    if d < min_distance:
                        min_distance = d
                        cpoint = Point((cp.position.x + cp2.position.x) / 2, (cp.position.y + cp2.position.y) / 2)
                        points.append(cp.position)
                        points.append(cp2.position)
                        break
                if cpoint is not None:
                    break
            if cpoint is not None:
                points.append(cpoint)

        packages = itertools.chain(self.blue_ato.packages,
                                   self.red_ato.packages)
        for package in packages:
            if package.primary_task is FlightType.BARCAP:
                # BARCAPs will be planned at most locations on smaller theaters,
                # rendering culling fairly useless. BARCAP packages don't really
                # need the ground detail since they're defensive. SAMs nearby
                # are only interesting if there are enemies in the area, and if
                # there are they won't be culled because of the enemy's mission.
                continue
            points.append(package.target.position)

        # Else 0,0, since we need a default value
        # (in this case this means the whole map is owned by the same player, so it is not an issue)
        if len(points) == 0:
            points.append(Point(0, 0))

        self.__culling_points = points

    def add_destroyed_units(self, data):
        pos = Point(data["x"], data["z"])
        if self.theater.is_on_land(pos):
            self.__destroyed_units.append(data)

    def get_destroyed_units(self):
        return self.__destroyed_units

    def position_culled(self, pos):
        """
        Check if unit can be generated at given position depending on culling performance settings
        :param pos: Position you are tryng to spawn stuff at
        :return: True if units can not be added at given position
        """
        if self.settings.perf_culling == False:
            return False
        else:
            for c in self.__culling_points:
                if c.distance_to_point(pos) < self.settings.perf_culling_distance * 1000:
                    return False
            return True

    def get_culling_points(self):
        """
        Check culling points
        :return: List of culling points
        """
        return self.__culling_points

    # 1 = red, 2 = blue
    def get_player_coalition_id(self):
        return 2

    def get_enemy_coalition_id(self):
        return 1

    def get_player_coalition(self):
        return Coalition.Blue

    def get_enemy_coalition(self):
        return Coalition.Red

    def get_player_color(self):
        return "blue"

    def get_enemy_color(self):
        return "red"

    def process_win_loss(self, turn_state: TurnState):
        if turn_state is TurnState.WIN:
            return self.message("Congratulations, you are victorious!  Start a new campaign to continue.")
        elif turn_state is TurnState.LOSS:
            return self.message("Game Over, you lose. Start a new campaign to continue.")
