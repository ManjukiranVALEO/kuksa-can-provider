#!/usr/bin/python3

########################################################################
# Copyright (c) 2020,2023 Contributors to the Eclipse Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0
########################################################################

"""
Classes for maintaining mapping between CAN frames and VSS signals
as well as performing the transformation of CAN signal values to/from
VSS signal values as defined in a mapping.
"""

import json
import logging
import sys
from typing import Any, Dict, List, Set, Optional, KeysView
from dataclasses import dataclass

from py_expression_eval import Parser  # type: ignore[import]

from dbcfeederlib import dbcparser

log = logging.getLogger(__name__)


@dataclass
class VSSObservation:
    """
    A VSSObservation is a container for a single observation/data for a single VSS signal.
    The data contained is the raw data as received on CAN, it has not yet been transformed
    into VSS representation.
    """

    dbc_name: str
    vss_name: str
    raw_value: Any
    time: float


class VSSMapping:
    """
    The definition of how a particular VSS data entry is mapped to/from a particular
    CAN message signal.

    This mapping can be used to represent either dbc2vss or vss2dbc mapping.
    As of today just by looking at an instance of this class you cannot say
    which direction it concerns.

    This implementation is supposed to match the
    [Kuksa Feeders documentation](https://github.com/eclipse/kuksa.val.feeders/blob/main/dbc2val/mapping/mapping.md)
    """

    parser: Parser = Parser()

    def __init__(self, vss_name: str, dbc_name: str, transform: dict, interval_ms: int,
                 on_change: bool, datatype: str, description: str):
        self.vss_name = vss_name
        self.dbc_name = dbc_name
        self.transform = transform
        self.interval_ms = interval_ms
        self.on_change = on_change
        self.datatype = datatype
        self.description = description
        # For time comparison (interval_ms) we store last value used for comparison. Unit seconds.
        self.last_time: float = 0.0
        # For value comparison (on_changes) we store last value used for comparison
        self.last_vss_value: Any = None
        self.last_dbc_value: Any = None

    def time_condition_fulfilled(self, time: float) -> bool:
        """
        Checks if time condition to send signal is fulfilled
        Value (on_change) condition not evaluated
        """
        fulfilled = True
        log.debug(f"Checking interval for {self.vss_name}. "
                  f"Time is {time}, last sent {self.last_time}")

        # First shall always evaluate to true
        if (self.interval_ms > 0) and (self.last_time != 0.0):
            diff_ms = (time - self.last_time) * 1000.0
            if diff_ms < self.interval_ms:
                log.debug(f"Interval not exceeded for {self.vss_name}. Time is {time}")
                fulfilled = False

        # We must set time already now even if a value check is performed later
        # Reason is that value condition is evaluated later after queuing
        if fulfilled:
            self.last_time = time

        return fulfilled

    def change_condition_fulfilled(self, vss_value: Any) -> bool:
        """
        Checks if change condition to send signal is fulfilled.
        Transformation is expected to be costly, so transformation and value check only performed
        if time condition is fulfilled.
        """
        fulfilled = False
        log.debug(f"Checking change condition for {self.vss_name}. "
                  f"New value {vss_value}, old value {self.last_vss_value}")

        if vss_value is not None:
            if self.last_vss_value is None:
                # Always send first value
                fulfilled = True
            elif self.on_change:
                fulfilled = self.last_vss_value != vss_value
            else:
                fulfilled = True

        if fulfilled:
            self.last_vss_value = vss_value
        return fulfilled

    def transform_value(self, value: Any) -> Any:
        """
        Transforms the given "raw" DBC value to the wanted VSS value.
        For now does not make any type checks
        """
        vss_value = None
        if self.transform is None:
            log.debug(f"No mapping to VSS {self.vss_name}, using raw value {value}")
            vss_value = value
        else:
            if "mapping" in self.transform:
                tmp = self.transform["mapping"]
                # Assumed to be a list
                for item in tmp:
                    from_val = item["from"]
                    if from_val == value:
                        new_val = item["to"]
                        vss_value = new_val
                        break
            elif "math" in self.transform:
                tmp = self.transform["math"]
                try:
                    vss_value = VSSMapping.parser.parse(tmp).evaluate({"x": value})
                except Exception:
                    # It is assumed that you may consider it ok that transformation fails sometimes,
                    # so giving warning instead of error
                    # This could be e.g. trying to treat a string as int
                    log.warning(f"Transformation failed for value {value} "
                                f"for VSS signal {self.vss_name}, signal ignored!", exc_info=True)
            else:
                # It is supposed that "extract_verify_transform" already have checked that
                # we have a valid transform, so we shall never end up here
                log.error("Unsupported transform")

        if vss_value is None:
            log.info(f"No mapping to VSS {self.vss_name} found for raw value {value},"
                     f"returning None to indicate that it shall be ignored!")
        else:
            log.debug(f"Transformed value {vss_value} for {self.vss_name}")
        return vss_value


class Mapper(dbcparser.DBCParser):
    """
    Contains all mappings between CAN and VSS signals.

    For a given signal from a CAN frame this mapper determines the set of VSS signals that the
    CAN signal is mapped to and computes the VSS signals' (current) value based on the mapping definition(s).

    For a given VSS signal's (target) this mapper determines the set of CAN frames that the VSS signal
    is mapped to and computes the CAN signals' value based on the mapping definition(s).
    """

    def __init__(self,
                 mapping_definitions_file: str,
                 dbc_file_names: List[str],
                 expect_extended_frame_ids: bool = False,
                 use_strict_parsing: bool = False,
                 can_signal_default_values_file: Optional[str] = None):

        super().__init__(dbc_file_names, use_strict_parsing, expect_extended_frame_ids)
        with open(mapping_definitions_file, "r") as file:
            try:
                jsonmapping = json.load(file)
                log.info("Reading CAN<->VSS mapping definitions from file %s", mapping_definitions_file)
            except Exception:
                log.error(
                    "Failed to read CAN<->VSS mapping definitions from file %s",
                    mapping_definitions_file, exc_info=True
                )
                sys.exit(-1)

        self.dbc_default = {}
        if can_signal_default_values_file is not None:
            with open(can_signal_default_values_file, "r") as file:
                try:
                    self.dbc_default = json.load(file)
                    log.info("Reading default CAN signal values from file %s", can_signal_default_values_file)
                except Exception:
                    log.error(
                        "Failed to read default CAN signal values from file %s",
                        can_signal_default_values_file, exc_info=True
                    )
                    sys.exit(-1)

        # Where we keep mapping, key is dbc signal name
        self._dbc2vss_mapping: Dict[str, List[VSSMapping]] = {}
        # In this direction key is VSS name
        self._vss2dbc_mapping: Dict[str, List[VSSMapping]] = {}
        # Same, but key is CAN id mapping
        self._vss2dbc_can_id_mapping: Dict[int, List[VSSMapping]] = {}

        self._traverse_vss_node("", jsonmapping)

    def transform_dbc_value(self, vss_observation: VSSObservation) -> Any:
        """
        Find VSS mapping and transform DBC value to VSS value.
        """
        vss_signal = self.get_dbc2vss_mapping(vss_observation.dbc_name, vss_observation.vss_name)
        if vss_signal:
            value = vss_signal.transform_value(vss_observation.raw_value)
            log.debug(f"Transformed dbc {vss_observation.dbc_name} to VSS "
                      f"{vss_observation.vss_name}, "
                      f"from raw value {vss_observation.raw_value} to {value}")
        else:
            log.error("No mapping found, that is not expected!")
            value = None
        return value

    def _extract_verify_transform(self, expanded_name: str, node: dict):
        """
        Extract transformation definition and check syntax.
        """
        if "transform" not in node:
            log.debug(f"No transformation found for {expanded_name}")
            # For now assumed that None is Ok
            return None
        transform = node["transform"]

        has_mapping = False

        if not isinstance(transform, dict):
            log.error(f"Transform not dict for {expanded_name}")
            sys.exit(-1)
        if "mapping" in transform:
            tmp = transform["mapping"]
            if not isinstance(tmp, list):
                log.error(f"Transform mapping not list for {expanded_name}")
                sys.exit(-1)
            for item in tmp:
                if not (("from" in item) and ("to" in item)):
                    log.error(f"Mapping missing to and from in {item} for {expanded_name}")
                    sys.exit(-1)
            has_mapping = True

        if "math" in transform:
            if has_mapping:
                log.error(f"Can not have both mapping and math for {expanded_name}")
                sys.exit(-1)
            if not isinstance(transform["math"], str):
                log.error(f"Math must be str for {expanded_name}")
                sys.exit(-1)
        elif not has_mapping:
            log.error(f"Unsupported transform for {expanded_name}")
            sys.exit(-1)
        return transform

    def _analyze_dbc2vss(self, expanded_name, node: dict, dbc2vss: dict):
        """
        Analyze a dbc2vss entry (from CAN to VSS).
        """

        transform = self._extract_verify_transform(expanded_name, dbc2vss)
        dbc_name = dbc2vss.get("signal", "")
        if dbc_name == "":
            log.error(f"No dbc signal found for {expanded_name}")
            sys.exit(-1)
        on_change: bool = False
        if "on_change" in dbc2vss:
            tmp = dbc2vss["on_change"]
            if isinstance(tmp, bool):
                on_change = tmp
            else:
                log.error(f"Value for on_change ({tmp}) is not bool")
                sys.exit(-1)
        if "interval_ms" in dbc2vss:
            interval = dbc2vss["interval_ms"]
            if not isinstance(interval, int):
                log.error(f"Faulty interval for {expanded_name}")
                sys.exit(-1)
        else:
            if on_change:
                log.info(f"Using default interval 0 ms for {expanded_name} "
                         f"as it has on_change condition")
                interval = 0
            else:
                log.info(f"Using default interval 1000 ms for {expanded_name}")
                interval = 1000
        mapping_entry = VSSMapping(expanded_name, dbc_name, transform, interval, on_change,
                                   node["datatype"], node["description"])
        if dbc_name not in self._dbc2vss_mapping:
            self._dbc2vss_mapping[dbc_name] = []
        self._dbc2vss_mapping[dbc_name].append(mapping_entry)

    def _analyze_vss2dbc(self, expanded_name, node: dict, vss2dbc: dict):
        """
        Analyze a vss2dbc entry (from VSS to CAN).
        """

        transform = self._extract_verify_transform(expanded_name, vss2dbc)
        dbc_name = vss2dbc.get("signal", "")
        if dbc_name == "":
            log.error(f"No dbc signal found for {expanded_name}")
            sys.exit(-1)
        # For now we only support on_change, and we actually do not use the values
        on_change: bool = True
        interval = 0
        if "on_change" in vss2dbc:
            log.warning(f"on_change attribute ignored for {expanded_name}")
        if "interval_ms" in vss2dbc:
            log.warning(f"interval_ms attribute ignored for {expanded_name}")

        mapping_entry = VSSMapping(expanded_name, dbc_name, transform, interval, on_change,
                                   node["datatype"], node["description"])
        if dbc_name not in self._vss2dbc_mapping:
            self._vss2dbc_mapping[expanded_name] = []
        self._vss2dbc_mapping[expanded_name].append(mapping_entry)

        # Also add CAN-id
        dbc_can_id = self.get_canid_for_signal(dbc_name)
        if not dbc_can_id:
            log.error(f"Could not find {dbc_name}")
            return
        if dbc_can_id not in self._vss2dbc_can_id_mapping:
            self._vss2dbc_can_id_mapping[dbc_can_id] = []
        self._vss2dbc_can_id_mapping[dbc_can_id].append(mapping_entry)

    def _analyze_signal(self, expanded_name, node):
        """
        Analyze a VSS signal definition and add mapping entry if correct mapping found.
        """
        dbc2vss_def = None
        if "dbc" in node:
            log.debug(f"Signal {expanded_name} has dbc!")
            dbc2vss_def = node["dbc"]
            if "dbc2vss" in node:
                log.error(f"Node {expanded_name} has both dbc and dbc2vss")
                sys.exit(-1)
        elif "dbc2vss" in node:
            log.debug(f"Signal {expanded_name} has dbc2vss!")
            dbc2vss_def = node["dbc2vss"]
        if dbc2vss_def is not None:
            self._analyze_dbc2vss(expanded_name, node, dbc2vss_def)
        if "vss2dbc" in node:
            self._analyze_vss2dbc(expanded_name, node, node["vss2dbc"])

    def _traverse_vss_node(self, name, node, prefix=""):
        """
        Traverse a VSS node/tree and order all found VSS signals to be analyzed
        so that mapping can be extracted.
        """
        is_signal = False
        is_branch = False
        expanded_name = ""
        if isinstance(node, dict):
            if "type" in node:
                if node["type"] in ["sensor", "actuator", "attribute"]:
                    is_signal = True
                elif node["type"] in ["branch"]:
                    is_branch = True
                    prefix = prefix + name + "."

        # Assuming it to be a dict
        if is_branch:
            for item in node["children"].items():
                self._traverse_vss_node(item[0], item[1], prefix)
        elif is_signal:
            expanded_name = prefix + name
            self._analyze_signal(expanded_name, node)
        elif isinstance(node, dict):
            for item in node.items():
                self._traverse_vss_node(item[0], item[1], prefix)

    def get_dbc2vss_mapping(self, dbc_name: str, vss_name: str) -> Optional[VSSMapping]:
        """
        Helper method for test purposes
        """
        if dbc_name in self._dbc2vss_mapping:
            for mapping in self._dbc2vss_mapping[dbc_name]:
                if mapping.vss_name == vss_name:
                    return mapping
        return None

    def get_dbc2vss_entries(self) -> KeysView[str]:
        """Get all CAN signal names for which a mapping to a VSS Data Entry exists."""
        return self._dbc2vss_mapping.keys()

    def get_vss2dbc_entries(self) -> KeysView[str]:
        """Get all VSS Data Entry paths for which a mapping to a CAN signal name exists."""
        return self._vss2dbc_mapping.keys()

    def get_vss_names(self) -> Set[str]:
        """Get all VSS names used in mappings, both vss2dbc and dbc2vss"""
        vss_names = set()
        for entry in self._dbc2vss_mapping.values():
            for vss_mapping in entry:
                vss_names.add(vss_mapping.vss_name)
        for key_entry in self._vss2dbc_mapping.keys():
            vss_names.add(key_entry)
        return vss_names

    def has_dbc2vss_mapping(self) -> bool:
        return bool(self._dbc2vss_mapping)

    def has_vss2dbc_mapping(self) -> bool:
        return bool(self._vss2dbc_mapping)

    def get_dbc2vss_mappings(self, dbc_name: str) -> List[VSSMapping]:
        if dbc_name in self._dbc2vss_mapping:
            return self._dbc2vss_mapping[dbc_name]
        return []

    def handle_update(self, vss_name, value: Any) -> Set[str]:
        """
        Finds dbc signals using this VSS-signal, transform value accordingly
        and updated stored value.
        Returns set of affected CAN signal identifiers.
        Types of values tested so far: int, bool
        """
        dbc_ids = set()
        # Theoretically there might me multiple DBC-signals served by this VSS-signal
        for dbc_mapping in self._vss2dbc_mapping[vss_name]:

            dbc_value = dbc_mapping.transform_value(value)
            dbc_mapping.last_dbc_value = dbc_value
            dbc_ids.add(dbc_mapping.dbc_name)
        return dbc_ids

    def get_default_values(self, can_id) -> Dict[str, Any]:

        res = {}
        for signal in self.get_signals_for_canid(can_id):
            if signal in self.dbc_default:
                res[signal] = self.dbc_default[signal]
            else:
                log.error(f"No default value for  {signal} in CAN id {can_id}")
        return res

    def get_value_dict(self, can_id):

        log.debug(f"Using stored information to create CAN-frame for {can_id}")
        res = self.get_default_values(can_id)
        for can_mapping in self._vss2dbc_can_id_mapping[can_id]:
            log.debug(f"Using DBC id {can_mapping.dbc_name} with value {can_mapping.last_dbc_value}")
            if can_mapping.last_dbc_value is not None:
                res[can_mapping.dbc_name] = can_mapping.last_dbc_value
        return res

    def __contains__(self, key):
        return key in self._dbc2vss_mapping.keys()

    def __getitem__(self, item):
        return self._dbc2vss_mapping[item]
