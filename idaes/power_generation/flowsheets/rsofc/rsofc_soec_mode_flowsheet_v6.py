###############################################################################
# The Institute for the Design of Advanced Energy Systems Integrated Platform
# Framework (IDAES IP) was produced under the DOE Institute for the
# Design of Advanced Energy Systems (IDAES), and is copyright (c) 2018-2021
# by the software owners: The Regents of the University of California, through
# Lawrence Berkeley National Laboratory,  National Technology & Engineering
# Solutions of Sandia, LLC, Carnegie Mellon University, West Virginia
# University Research Corporation, et al.  All rights reserved.
#
# Please see the files COPYRIGHT.md and LICENSE.md for full copyright and
# license information.
###############################################################################

__author__ = "Chinedu Okoli", "John Eslick", "Alex Noring"

# Import python modules
import os
# import csv
# import numpy as np
# import matplotlib.pyplot as plt

# Import pyomo modules
import pyomo.environ as pyo
from pyomo.util.calc_var_value import calculate_variable_from_constraint
from pyomo.network import Arc, Port
from pyomo.common.fileutils import this_file_dir
# import pyomo.common.errors

# Import modules from core
from idaes.core import FlowsheetBlock
import idaes.core.util as iutil
import idaes.core.util.tables as tables
import idaes.core.util.scaling as iscale
import idaes.core.util.initialization as iinit
from idaes.core.util import copy_port_values
import idaes.core.plugins

# Import unit models
from idaes.power_generation.unit_models.helm import (
    # HelmMixer,
    # MomentumMixingType,
    HelmSplitter,
    HelmIsentropicCompressor
)
import idaes.generic_models.unit_models as gum  # generic unit models
import idaes.power_generation.unit_models as pum  # power unit models
from idaes.power_generation.flowsheets.sofc.surrogates.cpu import CPU
from idaes.generic_models.unit_models.heat_exchanger import (
    delta_temperature_underwood_callback
)
from idaes.generic_models.unit_models.pressure_changer import \
    ThermodynamicAssumption
from idaes.generic_models.unit_models.separator import SplittingType

# Import properties
from idaes.generic_models.properties.core.generic.generic_property import (
    GenericParameterBlock,
)
from idaes.generic_models.properties.core.generic.generic_reaction import (
    GenericReactionParameterBlock,
)
from idaes.power_generation.properties.natural_gas_PR import (
    get_prop, get_rxn, EosType)
from idaes.power_generation.flowsheets.rsofc.properties. \
    CO2_H2O_Ideal_VLE_scaled import configuration as CO2_H2O_VLE_config
from idaes.core.solvers import use_idaes_solver_configuration_defaults
from idaes.generic_models.properties import iapws95
# from idaes.generic_models.properties.helmholtz.helmholtz import (
#     HelmholtzThermoExpressions as ThermoExpr,
# )

import idaes.logger as idaeslog


def _set_port(port, F, T, P, comp, fix=True):
    if fix:
        port.flow_mol.fix(F)
        port.temperature.fix(T)
        port.pressure.fix(P)
        for k, v in comp.items():
            port.mole_frac_comp[:, k].fix(v)
    else:
        port.flow_mol[:].value = F
        port.temperature[:].value = T
        port.pressure[:] = P
        for k, v in comp.items():
            port.mole_frac_comp[:, k].value = v


def add_flowsheet(m=None, name="SOEC Module"):
    if m is None:
        m = pyo.ConcreteModel(name)
    if not hasattr(m, "fs"):
        m.fs = FlowsheetBlock(default={"dynamic": False})
    return m


def add_properties(m):
    fuel_comp = {  # components present
        "CH4",
        "C2H6",
        "C3H8",
        "C4H10",
        "O2",
        "H2O",
        "CO2",
        "N2",
        "Ar",
    }
    air_comp = {  # components present
        "O2",
        "H2O",
        "CO2",
        "N2",
        "Ar",
    }
    m.fs.fg_prop = GenericParameterBlock(
        default=get_prop(components=fuel_comp, phases=["Vap"],
                         eos=EosType.IDEAL)
    )
    m.fs.fg_prop.set_default_scaling("mole_frac_comp", 1e2)
    m.fs.fg_prop.set_default_scaling("mole_frac_phase_comp", 1e2)
    rxns = {  # reactions and key components for conversion
        "ch4_cmb": "CH4",
        "c2h6_cmb": "C2H6",
        "c3h8_cmb": "C3H8",
        "c4h10_cmb": "C4H10",
    }
    m.rxns = rxns
    m.fs.fg_combust = GenericReactionParameterBlock(
        default=get_rxn(m.fs.fg_prop, rxns))
    m.fs.water_prop = iapws95.Iapws95ParameterBlock()
    m.fs.h2_prop = GenericParameterBlock(
        default=get_prop(components={"H2", "H2O"},
                         phases=["Vap"], eos=EosType.IDEAL)
    )
    m.fs.o2_prop = GenericParameterBlock(
        default=get_prop(components={"O2", "H2O"},
                         phases=["Vap"], eos=EosType.IDEAL)
    )
    m.fs.air_prop = GenericParameterBlock(
        default=get_prop(components=air_comp,
                         phases=["Vap"], eos=EosType.IDEAL)
    )
    m.fs.air_prop.set_default_scaling("mole_frac_comp", 1e2)
    m.fs.air_prop.set_default_scaling("mole_frac_phase_comp", 1e2)
    m.fs.h2_compress_prop = GenericParameterBlock(
        default=get_prop(components={"H2"}, phases=["Vap"], eos=EosType.PR)
    )
    m.fs.h2_compress_prop.set_default_scaling("mole_frac_comp", 1e2)
    m.fs.h2_compress_prop.set_default_scaling("mole_frac_phase_comp", 1e2)

    m.fs.CO2_H2O_VLE = GenericParameterBlock(default=CO2_H2O_VLE_config)
    m.fs.CO2_H2O_VLE.set_default_scaling("mole_frac_comp", 1e2)
    m.fs.CO2_H2O_VLE.set_default_scaling("mole_frac_phase_comp", 1e2)


def add_asu(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.air_compressor_s1 = gum.PressureChanger(
        default={"compressor": True,
                 "property_package": m.fs.air_prop,
                 "thermodynamic_assumption":
                     ThermodynamicAssumption.isentropic})

    m.fs.intercooler_s1 = gum.Heater(
        default={"property_package": m.fs.air_prop,
                 "has_pressure_change": True})

    m.fs.air_compressor_s2 = gum.PressureChanger(
        default={"compressor": True,
                 "property_package": m.fs.air_prop,
                 "thermodynamic_assumption":
                     ThermodynamicAssumption.isentropic})

    m.fs.intercooler_s2 = gum.Heater(
        default={"property_package": m.fs.air_prop,
                 "has_pressure_change": True})

    m.fs.ASU = gum.Separator(
        default={"outlet_list": ["N2_outlet", "O2_outlet"],
                 "split_basis": SplittingType.componentFlow,
                 "property_package": m.fs.air_prop})

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    # air compressors and intercoolers
    m.fs.air_compressor_s1.outlet.pressure.fix(111422)  # Pa (34 psia)
    m.fs.air_compressor_s1.efficiency_isentropic.fix(0.84)
    m.fs.intercooler_s1.outlet.temperature.fix(310.93)  # K (100 F)
    m.fs.intercooler_s1.deltaP.fix(-3447)  # Pa (-0.5 psi)
    m.fs.air_compressor_s2.outlet.pressure.fix(130686)  # Pa (79 psia)
    m.fs.air_compressor_s2.efficiency_isentropic.fix(0.84)
    m.fs.intercooler_s2.outlet.temperature.fix(310.93)  # K (100 F)
    m.fs.intercooler_s2.deltaP.fix(-3447)  # Pa (-0.5 psi)

    # air seperation unit
    m.fs.ASU.split_fraction[0, "O2_outlet", "CO2"].fix(0)
    m.fs.ASU.split_fraction[0, "O2_outlet", "H2O"].fix(0)
    m.fs.ASU.split_fraction[0, "O2_outlet", "N2"].fix(0.0005)
    m.fs.ASU.split_fraction[0, "O2_outlet", "O2"].fix(0.9691)
    m.fs.ASU.split_fraction[0, "O2_outlet", "Ar"].fix(0.0673)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    # Arcs for ASU, oxycombustor and CPU
    m.fs.STAGE_1_OUT = Arc(
        source=m.fs.air_compressor_s1.outlet,
        destination=m.fs.intercooler_s1.inlet)

    m.fs.IC_1_OUT = Arc(
        source=m.fs.intercooler_s1.outlet,
        destination=m.fs.air_compressor_s2.inlet)

    m.fs.STAGE_2_OUT = Arc(
        source=m.fs.air_compressor_s2.outlet,
        destination=m.fs.intercooler_s2.inlet)

    m.fs.ba01 = Arc(
        source=m.fs.intercooler_s2.outlet,
        destination=m.fs.ASU.inlet)


def add_preheater(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.oxygen_preheater = gum.HeatExchanger(
        default={
            "delta_temperature_callback": delta_temperature_underwood_callback,
            "shell": {"property_package": m.fs.air_prop},
            "tube": {"property_package": m.fs.air_prop},
        }
    )
    m.fs.ng_preheater = gum.HeatExchanger(
        default={
            "delta_temperature_callback": delta_temperature_underwood_callback,
            "shell": {"property_package": m.fs.air_prop},
            "tube": {"property_package": m.fs.fg_prop},
        }
    )
    m.fs.preheat_split = gum.Separator(
        default={
            "property_package": m.fs.air_prop,
            "outlet_list": ["air", "ng"],
        }
    )

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    m.fs.oxygen_preheater.overall_heat_transfer_coefficient.fix(100)
    m.fs.oxygen_preheater.delta_temperature_in.fix(30)  # fix DT for pinch side

    m.fs.ng_preheater.overall_heat_transfer_coefficient.fix(100)
    m.fs.ng_preheater.delta_temperature_in.fix(30)  # fix DT for pinch side

    m.fs.preheat_split.split_fraction[:, "air"].fix(0.9)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.o04 = Arc(
        source=m.fs.preheat_split.ng,
        destination=m.fs.ng_preheater.shell_inlet
    )
    m.fs.o05 = Arc(
        source=m.fs.preheat_split.air,
        destination=m.fs.oxygen_preheater.shell_inlet
    )
    m.fs.ba02 = Arc(
        source=m.fs.ASU.O2_outlet,
        destination=m.fs.oxygen_preheater.tube_inlet
        )


def add_combustor(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.pre_oxycombustor_translator = gum.Translator(
        default={"outlet_state_defined": True,
                 "inlet_property_package": m.fs.air_prop,
                 "outlet_property_package": m.fs.fg_prop}
        )
    m.fs.cmb_mix = gum.Mixer(
        default={
            "property_package": m.fs.fg_prop,
            "inlet_list": ["ng", "air"],
            "momentum_mixing_type": gum.MomentumMixingType.none}
        )
    m.fs.cmb = gum.StoichiometricReactor(
        default={
            "property_package": m.fs.fg_prop,
            "reaction_package": m.fs.fg_combust,
            "has_pressure_change": False,
            "has_heat_of_reaction": True,
            "has_heat_transfer": True  # Use to add Q to water/steam in tubes
            }
        )
    m.fs.post_oxycombustor_translator = gum.Translator(
        default={"outlet_state_defined": True,
                 "inlet_property_package": m.fs.fg_prop,
                 "outlet_property_package": m.fs.air_prop}
        )

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    # Additional constraints to specify the pre combustor translator block
    @m.fs.pre_oxycombustor_translator.Constraint(m.fs.time)
    def pre_oxycombustor_translator_F(b, t):
        return b.inlet.flow_mol[t] == b.outlet.flow_mol[t]

    @m.fs.pre_oxycombustor_translator.Constraint(m.fs.time)
    def pre_oxycombustor_translator_T(b, t):
        return b.inlet.temperature[t] == b.outlet.temperature[t]

    @m.fs.pre_oxycombustor_translator.Constraint(m.fs.time)
    def pre_oxycombustor_translator_P(b, t):
        return b.inlet.pressure[t] == b.outlet.pressure[t]

    @m.fs.pre_oxycombustor_translator.Constraint(m.fs.time,
                                                 m.fs.air_prop.component_list)
    def pre_oxycombustor_translator_x(b, t, j):
        return b.inlet.mole_frac_comp[t, j] == b.outlet.mole_frac_comp[t, j]

    for j in m.fs.fg_prop.component_list:
        if j not in m.fs.air_prop.component_list:
            m.fs.pre_oxycombustor_translator.outlet.mole_frac_comp[0, j].fix(0)

    # Additional constraints to specify the mixer
    @m.fs.cmb_mix.Constraint(m.fs.time)
    def pressure_eqn(b, t):
        return b.mixed_state[t].pressure == b.ng_state[t].pressure

    # Additional constraints to specify the combustor reactions
    @m.fs.cmb.Constraint(m.fs.time, m.rxns.keys())
    def reaction_extent(b, t, r):
        k = m.rxns[r]
        prp = b.control_volume.properties_in[t]
        stc = -m.fs.fg_combust.rate_reaction_stoichiometry[r, "Vap", k]
        extent = b.rate_reaction_extent[t, r]
        return extent*stc == prp.flow_mol*prp.mole_frac_comp[k]

    # Additional constraints to specify the post combustor  translator block
    @m.fs.post_oxycombustor_translator.Constraint(m.fs.time)
    def post_oxycombustor_translator_F(b, t):
        return b.inlet.flow_mol[t] == b.outlet.flow_mol[t]

    @m.fs.post_oxycombustor_translator.Constraint(m.fs.time)
    def post_oxycombustor_translator_T(b, t):
        return b.inlet.temperature[t] == b.outlet.temperature[t]

    @m.fs.post_oxycombustor_translator.Constraint(m.fs.time)
    def post_oxycombustor_translator_P(b, t):
        return b.inlet.pressure[t] == b.outlet.pressure[t]

    @m.fs.post_oxycombustor_translator.Constraint(m.fs.time,
                                                  m.fs.air_prop.component_list)
    def post_oxycombustor_translator_x(b, t, j):
        return b.inlet.mole_frac_comp[t, j] == b.outlet.mole_frac_comp[t, j]

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.cmb_mix_in = Arc(  # TODO - rename arc
        source=m.fs.oxygen_preheater.tube_outlet,
        destination=m.fs.pre_oxycombustor_translator.inlet
    )
    m.fs.ba03 = Arc(
        source=m.fs.pre_oxycombustor_translator.outlet,
        destination=m.fs.cmb_mix.air
    )
    m.fs.bng01 = Arc(
        source=m.fs.ng_preheater.tube_outlet, destination=m.fs.cmb_mix.ng
    )
    m.fs.bng02 = Arc(
        source=m.fs.cmb_mix.outlet, destination=m.fs.cmb.inlet
    )
    m.fs.cmb_out = Arc(  # TODO - rename arc
        source=m.fs.cmb.outlet,
        destination=m.fs.post_oxycombustor_translator.inlet
    )
    m.fs.fg01 = Arc(source=m.fs.post_oxycombustor_translator.outlet,
                    destination=m.fs.air_preheater_2.shell_inlet)


def add_aux_boiler_steam(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.bhx2 = gum.Heater(  # in-bed heat exchanger to the oxycombustor
        default={"has_pressure_change": False,
                 "property_package": m.fs.water_prop})

    m.fs.main_steam_split = HelmSplitter(
        default={
            "property_package": m.fs.water_prop,
            "outlet_list": ["h_side", "o_side"]
        }
    )
    m.fs.aux_boiler_feed_pump = HelmIsentropicCompressor(
        default={"property_package": m.fs.water_prop}
    )
    m.fs.bhx1 = gum.HeatExchanger(
        default={
            "delta_temperature_callback": delta_temperature_underwood_callback,
            "shell": {"property_package": m.fs.h2_prop},
            "tube": {"property_package": m.fs.water_prop},
        }
    )

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    # m.fs.bhx2.shell_outlet.temperature[0].fix(700)
    # m.fs.bhx2.overall_heat_transfer_coefficient.fix(100)
    # m.fs.bhx1.area.fix(400)
    # htpx(T=None, P=None, x=None)
    # bhx2 - inbed combustor heat exchanger spec
    # TODO - Rewrite this spec in a more elegant way - Q comes from cmb
    h_bhx2 = pyo.value(iapws95.htpx(1073.15*pyo.units.K,1.1e5*pyo.units.Pa)) # enthalpy outlet
    m.fs.bhx2.outlet.enth_mol.fix(h_bhx2)  # K (100 F) # unfix after initalize and spec Q from cmb

    m.fs.bhx1.overall_heat_transfer_coefficient.fix(100)
    m.fs.bhx1.delta_temperature_out.fix(100)  # fix DT for pinch side

    m.fs.aux_boiler_feed_pump.outlet.pressure.fix(1.1e5)
    m.fs.aux_boiler_feed_pump.efficiency_isentropic.fix(0.85)

    m.fs.main_steam_split.split_fraction[:, "h_side"].fix(0.9999)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.s01 = Arc(
        source=m.fs.aux_boiler_feed_pump.outlet,
        destination=m.fs.bhx1.tube_inlet
    )
    m.fs.s02 = Arc(
        source=m.fs.bhx1.tube_outlet,
        destination=m.fs.bhx2.inlet
    )
    m.fs.s03 = Arc(
        source=m.fs.bhx2.outlet,
        destination=m.fs.main_steam_split.inlet
    )


def add_soec_air_side_units(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.air_blower = gum.PressureChanger(
        default={'compressor': True,
                 'property_package': m.fs.air_prop,
                 'thermodynamic_assumption':
                     ThermodynamicAssumption.isentropic}
    )
    m.fs.air_preheater_1 = gum.HeatExchanger(
        default={
            "delta_temperature_callback": delta_temperature_underwood_callback,
            "shell": {"property_package": m.fs.air_prop},
            "tube": {"property_package": m.fs.air_prop},
        }
    )
    m.fs.air_preheater_2 = gum.HeatExchanger(
        default={
            "delta_temperature_callback": delta_temperature_underwood_callback,
            "shell": {"property_package": m.fs.air_prop},
            "tube": {"property_package": m.fs.air_prop},
        }
    )
    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    m.fs.air_blower.outlet.pressure.fix(1.1e5)  # Pa (15.3 psi)
    m.fs.air_blower.efficiency_isentropic.fix(0.8)

    m.fs.air_preheater_1.overall_heat_transfer_coefficient.fix(100)
    m.fs.air_preheater_1.delta_temperature_in.fix(50)  # fix DT for pinch side

    m.fs.air_preheater_2.tube_outlet.temperature[0].fix(1073.15)  # Known value
    m.fs.air_preheater_2.overall_heat_transfer_coefficient.fix(100)
    # m.fs.air_preheater_2.area.fix(1000)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.a01 = Arc(source=m.fs.air_blower.outlet,
                   destination=m.fs.air_preheater_1.tube_inlet)
    m.fs.a02 = Arc(source=m.fs.air_preheater_1.tube_outlet,
                   destination=m.fs.air_preheater_2.tube_inlet)

    expand_arcs = pyo.TransformationFactory("network.expand_arcs")
    expand_arcs.apply_to(m)


def add_soec_unit(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.soec = pum.IsothermalSofc(
        default={
            "nz": 20,
            "nxfe": 10,
            "nxae": 10,
            "soec": True,
            "air_side_comp_list": ["O2", "H2O",
                                   "CO2", "N2", "Ar"],
            "fuel_side_comp_list": ["H2O", "H2"],
            "air_side_stoich": {"O2": -0.25, "H2O": 0,
                                "CO2": 0, "N2": 0, "Ar": 0},
        }
    )
    m.fs.spltf1 = gum.Separator(
        default={
            "property_package": m.fs.h2_prop,
            "outlet_list": ["out", "recycle"],
        }
    )
    m.fs.splta1 = gum.Separator(
        default={
            "property_package": m.fs.air_prop,
            "outlet_list": ["out", "recycle"],
        }
    )

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    # soec performance variables
    m.fs.soec.E_cell.fix(1.28)  # TODO - unfix after initialize
    m.fs.soec.el.thickness.fix(9e-6)
    m.fs.soec.fe.thickness.fix(1e-3)
    m.fs.soec.ae.thickness.fix(20e-6)
    m.fs.soec.length.fix(0.05)
    m.fs.soec.width.fix(0.05)
    m.fs.soec.k_ae.fix(26.1e7)
    m.fs.soec.eact_ae.fix(120000)
    m.fs.soec.alpha_ae.fix(0.4)
    m.fs.soec.k_fe.fix(1.35e10)
    m.fs.soec.eact_fe.fix(110000)
    m.fs.soec.alpha_fe.fix(0.5)
    m.fs.soec.fe.k_res.fix(2.98e-5)
    m.fs.soec.fe.E_res.fix(-1392)
    m.fs.soec.ae.k_res.fix(8.114e-5)
    m.fs.soec.ae.E_res.fix(600)
    m.fs.soec.el.k_res.fix(2.94e-5)
    m.fs.soec.el.E_res.fix(10350)
    m.fs.soec.fc.thickness.fix(0.002)
    m.fs.soec.ac.thickness.fix(0.002)
    m.fs.soec.fe.porosity.fix(0.48)
    m.fs.soec.fe.tortuosity.fix(5.4)
    m.fs.soec.ae.porosity.fix(0.48)
    m.fs.soec.ae.tortuosity.fix(5.4)
    temperature = 1073.15  # TODO - should temperatures be fixed here?
    m.fs.soec.el.temperature.fix(temperature)
    m.fs.soec.fc.temperature.fix(temperature)
    m.fs.soec.ac.temperature.fix(temperature)
    m.fs.soec.fe.temperature.fix(temperature)
    m.fs.soec.ae.temperature.fix(temperature)

    # performance variables for other units
    m.fs.spltf1.split_fraction[:, "out"].fix(0.98)
    m.fs.splta1.split_fraction[:, "out"].fix(0.9999)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.h01 = Arc(source=m.fs.soec.outlet_fc_mult,
                   destination=m.fs.spltf1.inlet)
    m.fs.o01 = Arc(source=m.fs.soec.outlet_ac_mult,
                   destination=m.fs.splta1.inlet)


def add_soec_inlet_mix(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.mxf1 = gum.Mixer(
        default={
            "property_package": m.fs.h2_prop,
            "inlet_list": ["water", "recycle"],
            "momentum_mixing_type": gum.MomentumMixingType.none,
        }
    )
    m.fs.mxa1 = gum.Mixer(
        default={
            "property_package": m.fs.air_prop,
            "inlet_list": ["air", "recycle"],
            "momentum_mixing_type": gum.MomentumMixingType.none,
        }
    )

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    @m.fs.mxf1.Constraint(m.fs.time)
    def fmxpress_eqn(b, t):
        return b.mixed_state[t].pressure == b.water_state[t].pressure

    @m.fs.mxa1.Constraint(m.fs.time)
    def amxpress_eqn(b, t):
        return b.mixed_state[t].pressure == b.air_state[t].pressure

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    # Add ports to connect pure steam to steam + h2
    m.fs.main_steam_split._temperature_h_side_ref = pyo.Reference(
        m.fs.main_steam_split.h_side_state[:].temperature
    )

    @m.fs.main_steam_split.Expression(m.fs.time, m.fs.soec.fc.config.comp_list)
    def h_side_mole_frac_expr(b, t, i):
        if i == "H2O":
            return 1
        else:
            return 0

    m.fs.main_steam_split.h_side_adapt = Port(
        rule=lambda b: {
            "flow_mol": m.fs.main_steam_split._flow_mol_h_side_ref,
            "pressure": m.fs.main_steam_split._pressure_h_side_ref,
            "temperature": m.fs.main_steam_split._temperature_h_side_ref,
            "mole_frac_comp": m.fs.main_steam_split.h_side_mole_frac_expr,
        }
    )

    m.fs.s10 = Arc(
        source=m.fs.main_steam_split.h_side_adapt,
        destination=m.fs.mxf1.water,
    )

    m.fs.hr01 = Arc(
        source=m.fs.spltf1.recycle,
        destination=m.fs.mxf1.recycle,
    )

    m.fs.s12 = Arc(source=m.fs.mxf1.outlet,
                   destination=m.fs.soec.inlet_fc_mult)
    m.fs.s13 = Arc(source=m.fs.mxa1.outlet,
                   destination=m.fs.soec.inlet_ac_mult)

    m.fs.a03 = Arc(
        source=m.fs.air_preheater_2.tube_outlet,
        destination=m.fs.mxa1.air
    )


def add_h2_compressor(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.hcmp_ic01 = gum.Heater(
                default={"property_package": m.fs.h2_compress_prop})
    m.fs.hcmp01 = gum.Compressor(
                default={"property_package": m.fs.h2_compress_prop,
                         "thermodynamic_assumption":
                             ThermodynamicAssumption.isentropic})
    m.fs.hcmp_ic02 = gum.Heater(
                default={"property_package": m.fs.h2_compress_prop})
    m.fs.hcmp02 = gum.Compressor(
                default={"property_package": m.fs.h2_compress_prop,
                         "thermodynamic_assumption":
                             ThermodynamicAssumption.isentropic})
    m.fs.hcmp_ic03 = gum.Heater(
                default={"property_package": m.fs.h2_compress_prop})
    m.fs.hcmp03 = gum.Compressor(
        default={"property_package": m.fs.h2_compress_prop,
                 "thermodynamic_assumption":
                     ThermodynamicAssumption.isentropic})
    m.fs.hcmp_ic04 = gum.Heater(
                default={"property_package": m.fs.h2_compress_prop})
    m.fs.hcmp04 = gum.Compressor(
                default={"property_package": m.fs.h2_compress_prop,
                         "thermodynamic_assumption":
                             ThermodynamicAssumption.isentropic})

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    @m.fs.bhx1.Expression(m.fs.time, {"H2"})
    def waterless_mole_frac_expr(b, t, i):
        return 1

    @m.fs.bhx1.Expression(m.fs.time)
    def waterless_flow_expr(b, t):
        return (
            m.fs.bhx1._flow_mol_shell_outlet_ref[t]
            * m.fs.bhx1._mole_frac_comp_shell_outlet_ref[t, "H2"]
        )

    m.fs.bhx1.shell_outlet_drop_water = Port(
        rule=lambda b: {
            "flow_mol": m.fs.bhx1.waterless_flow_expr,
            "pressure": m.fs.bhx1._pressure_shell_outlet_ref,
            "temperature": m.fs.bhx1._temperature_shell_outlet_ref,
            "mole_frac_comp": m.fs.bhx1.waterless_mole_frac_expr,
        }
    )

    m.fs.hcmp_ic01.outlet.temperature.fix(320)
    m.fs.hcmp01.outlet.pressure.fix(40e5)
    m.fs.hcmp01.efficiency_isentropic.fix(0.9)

    m.fs.hcmp_ic02.outlet.temperature.fix(320)
    m.fs.hcmp02.outlet.pressure.fix(80e5)
    m.fs.hcmp02.efficiency_isentropic.fix(0.9)

    m.fs.hcmp_ic03.outlet.temperature.fix(320)
    m.fs.hcmp03.outlet.pressure.fix(160e5)
    m.fs.hcmp03.efficiency_isentropic.fix(0.9)

    m.fs.hcmp_ic04.outlet.temperature.fix(320)
    m.fs.hcmp04.outlet.pressure.fix(320e5)
    m.fs.hcmp04.efficiency_isentropic.fix(0.9)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.h03 = Arc(
        source=m.fs.bhx1.shell_outlet_drop_water,
        destination=m.fs.hcmp_ic01.inlet
    )
    m.fs.h04 = Arc(source=m.fs.hcmp_ic01.outlet,
                   destination=m.fs.hcmp01.inlet)
    m.fs.h05 = Arc(source=m.fs.hcmp01.outlet,
                   destination=m.fs.hcmp_ic02.inlet)
    m.fs.h06 = Arc(source=m.fs.hcmp_ic02.outlet,
                   destination=m.fs.hcmp02.inlet)
    m.fs.h07 = Arc(source=m.fs.hcmp02.outlet,
                   destination=m.fs.hcmp_ic03.inlet)
    m.fs.h08 = Arc(source=m.fs.hcmp_ic03.outlet,
                   destination=m.fs.hcmp03.inlet)
    m.fs.h09 = Arc(source=m.fs.hcmp03.outlet,
                   destination=m.fs.hcmp_ic04.inlet)
    m.fs.h10 = Arc(source=m.fs.hcmp_ic04.outlet,
                   destination=m.fs.hcmp04.inlet)


def add_hrsg_and_cpu(m):
    ###########################################################################
    #  Build unit operations
    ###########################################################################
    m.fs.soec_air_HRSG_1 = gum.Heater(
        default={"has_pressure_change": False,
                 "property_package": m.fs.air_prop})
    m.fs.soec_air_HRSG_2 = gum.Heater(
        default={"has_pressure_change": False,
                 "property_package": m.fs.air_prop})
    m.fs.fluegas_HRSG = gum.Heater(
        default={"has_pressure_change": False,
                 "property_package": m.fs.air_prop})

    m.fs.CPU_translator = gum.Translator(
        default={"outlet_state_defined": True,
                 "inlet_property_package": m.fs.air_prop,
                 "outlet_property_package": m.fs.CO2_H2O_VLE})
    m.fs.condenser = gum.Heater(
        default={"has_pressure_change": False,
                 "property_package": m.fs.CO2_H2O_VLE})

    m.fs.flash = gum.Flash(
        default={"has_heat_transfer": True,
                 "has_pressure_change": True,
                 "property_package": m.fs.CO2_H2O_VLE})
    m.fs.CPU = CPU()

    ###########################################################################
    #  Specify performance variables of unit operations
    ###########################################################################
    m.fs.soec_air_HRSG_1.outlet.temperature.fix(405)  # K

    m.fs.soec_air_HRSG_2.outlet.temperature.fix(405)  # K

    m.fs.fluegas_HRSG.outlet.temperature.fix(405)  # K

    m.fs.condenser.outlet.temperature.fix(310.9)  # K (100 F)

    m.fs.flash.control_volume.properties_out[0].temperature.fix(310.9)  # K
    m.fs.flash.control_volume.properties_out[0].pressure.fix(1013529)  # Pa

    # CPU translator constraints
    @m.fs.CPU_translator.Constraint(m.fs.time)
    def CPU_translator_F(b, t):
        return b.inlet.flow_mol[t] == b.outlet.flow_mol[t]

    @m.fs.CPU_translator.Constraint(m.fs.time)
    def CPU_translator_T(b, t):
        return b.inlet.temperature[t] == b.outlet.temperature[t]

    @m.fs.CPU_translator.Constraint(m.fs.time)
    def CPU_translator_P(b, t):
        return b.inlet.pressure[t] == b.outlet.pressure[t]

    @m.fs.CPU_translator.Constraint(m.fs.time, m.fs.CO2_H2O_VLE.component_list)
    def CPU_translator_x(b, t, j):
        return b.inlet.mole_frac_comp[t, j] == b.outlet.mole_frac_comp[t, j]
    for j in m.fs.air_prop.component_list:
        if j not in m.fs.CO2_H2O_VLE.component_list:
            m.fs.pre_oxycombustor_translator.outlet.mole_frac_comp[0, j].fix(0)

    ###########################################################################
    #  Add stream connections
    ###########################################################################
    m.fs.a06 = Arc(
        source=m.fs.oxygen_preheater.shell_outlet,
        destination=m.fs.soec_air_HRSG_1.inlet
    )
    m.fs.a07 = Arc(
        source=m.fs.ng_preheater.shell_outlet,
        destination=m.fs.soec_air_HRSG_2.inlet
    )
    m.fs.fg02 = Arc(source=m.fs.air_preheater_2.shell_outlet,
                    destination=m.fs.fluegas_HRSG.inlet)
    m.fs.fg03 = Arc(source=m.fs.fluegas_HRSG.outlet,
                    destination=m.fs.CPU_translator.inlet)
    m.fs.fg04 = Arc(source=m.fs.CPU_translator.outlet,
                    destination=m.fs.condenser.inlet)
    m.fs.fg05 = Arc(source=m.fs.condenser.outlet,
                    destination=m.fs.flash.inlet)

    # TODO - copy_port method used instead of propagate_state because of issue
    # TODO - noticed in tags with propagating flash.vap_outlet to CPU.inlet
    # m.fs.fg06 = Arc(source=m.fs.flash.vap_outlet,
    #                 destination=m.fs.CPU.inlet)
    @m.fs.Constraint(m.fs.time)
    def CPU_inlet_F(fs, t):
        return (1e-3*fs.CPU.inlet.flow_mol[t] ==
                1e-3*fs.flash.vap_outlet.flow_mol[t])

    @m.fs.Constraint(m.fs.time)
    def CPU_inlet_T(fs, t):
        return (1e-3*fs.CPU.inlet.temperature[t] ==
                1e-3*fs.flash.vap_outlet.temperature[t])

    @m.fs.Constraint(m.fs.time)
    def CPU_inlet_P(fs, t):
        return (1e-3*fs.CPU.inlet.pressure[t] ==
                1e-3*fs.flash.vap_outlet.pressure[t])

    @m.fs.Constraint(m.fs.time, m.fs.CO2_H2O_VLE.component_list)
    def CPU_inlet_x(fs, t, j):
        return (1e2*fs.CPU.inlet.mole_frac_comp[t, j] ==
                1e2*fs.flash.vap_outlet.mole_frac_comp[t, j])


def add_more_hx_connections(m):
    m.fs.h02 = Arc(source=m.fs.spltf1.out,
                   destination=m.fs.bhx1.shell_inlet)
    m.fs.o02 = Arc(source=m.fs.splta1.out,
                   destination=m.fs.air_preheater_1.shell_inlet)
    m.fs.o03 = Arc(
        source=m.fs.air_preheater_1.shell_outlet,
        destination=m.fs.preheat_split.inlet
        )


def add_design_constraints(m):
    m.fs.soec_heat_duty = pyo.Var(m.fs.time, units=pyo.units.W)

    @m.fs.Constraint(m.fs.time)
    def heat_duty_soec_zero_eqn(b, t):
        return b.soec.heat_duty[t] == b.soec_heat_duty[t]

    m.fs.cmb_temperature = pyo.Var(m.fs.time, initialize=2000,
                                   units=pyo.units.K)

    @m.fs.Constraint(m.fs.time)
    def cmb_temperature_eqn(b, t):
        return m.fs.cmb.outlet.temperature[t] == m.fs.cmb_temperature[t]

    m.fs.soec_steam_temperature = pyo.Var(m.fs.time, initialize=1073.15,
                                          units=pyo.units.K)

    @m.fs.Constraint(m.fs.time)
    def soec_steam_temperature_eqn(b, t):
        return (m.fs.bhx2.control_volume.properties_out[t].temperature ==
                m.fs.soec_steam_temperature[t])

    @m.fs.Constraint(m.fs.time)
    def air_to_combustor(b, t):
        XS = 1.09  # excess oxygen

        F_CH4 = (m.fs.ng_preheater.tube_inlet.flow_mol[t] *
                 m.fs.ng_preheater.tube_inlet.mole_frac_comp[(t, "CH4")])
        F_C2H6 = (m.fs.ng_preheater.tube_inlet.flow_mol[t] *
                  m.fs.ng_preheater.tube_inlet.mole_frac_comp[(t, "C2H6")])
        F_C3H8 = (m.fs.ng_preheater.tube_inlet.flow_mol[t] *
                  m.fs.ng_preheater.tube_inlet.mole_frac_comp[(t, "C3H8")])
        F_C4H10 = (m.fs.ng_preheater.tube_inlet.flow_mol[t] *
                   m.fs.ng_preheater.tube_inlet.mole_frac_comp[(t, "C4H10")])
        O2_required = 2*F_CH4 + 3.5*F_C2H6 + 5*F_C3H8 + 6.5*F_C4H10

        O2_fed = (m.fs.air_compressor_s1.inlet.flow_mol[t] *
                  m.fs.air_compressor_s1.inlet.mole_frac_comp[(t, "O2")])
        return 1e-1*O2_fed == 1e-1*XS*O2_required

    # TODO - add constraint for cmb Q to in-bed heat exchanger (produces steam)
    @m.fs.Constraint(m.fs.time)
    def combustor_heat(b, t):
        return m.fs.cmb.heat_duty[t] == -1 * (m.fs.bhx2.heat_duty[t])


def add_result_constraints(m):
    m.fs.soec_power = pyo.Var(m.fs.time,
                              units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def soec_power_constraint(b, t):
        return b.soec_power[t] == pyo.units.convert(
            b.soec.total_power[t], pyo.units.MW)

    @m.fs.Expression(m.fs.time)
    def hydrogen_product_rate_expr(b, t):
        return (
            b.bhx1.shell_outlet.flow_mol[t]
            * b.bhx1.shell_outlet.mole_frac_comp[t, "H2"]
        )

    m.fs.hydrogen_product_rate = pyo.Var(m.fs.time,
                                         units=pyo.units.mol / pyo.units.s)

    @m.fs.Constraint(m.fs.time)
    def hydrogen_product_rate_constraint(b, t):
        return b.hydrogen_product_rate[t] == b.hydrogen_product_rate_expr[t]

    @m.fs.Expression(m.fs.time)
    def h2_product_rate_mass(b, t):  # kg/s
        return (m.fs.hydrogen_product_rate[t] *
                0.002 * pyo.units.kg / pyo.units.mol)

    @m.fs.Expression(m.fs.time)
    def soec_power_per_h2(b, t):
        return (
            b.soec.total_power[t]
            / b.hydrogen_product_rate_expr[t]
            / (0.002 * pyo.units.kg / pyo.units.mol)
        )

    @m.fs.Expression(m.fs.time)
    def h2_compressor_power_expr(b, t):
        return (
            m.fs.hcmp01.control_volume.work[t]
            + m.fs.hcmp02.control_volume.work[t]
            + m.fs.hcmp03.control_volume.work[t]
            + m.fs.hcmp04.control_volume.work[t]
        )

    m.fs.h2_compressor_power = pyo.Var(m.fs.time,
                                       units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def h2_compressor_power_constraint(b, t):
        return b.h2_compressor_power[t] == pyo.units.convert(
            b.h2_compressor_power_expr[t], pyo.units.MW)

    # total heat supplied to HRSG
    m.fs.HRSG_heat_duty = pyo.Var(m.fs.time,
                                  initialize=300, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def HRSG_heat_duty_constraint(fs, t):
        return (
            -1*fs.HRSG_heat_duty[t] ==
            pyo.units.convert(fs.soec_air_HRSG_1.heat_duty[t], pyo.units.MW) +
            pyo.units.convert(fs.soec_air_HRSG_2.heat_duty[t], pyo.units.MW) +
            pyo.units.convert(fs.fluegas_HRSG.heat_duty[t], pyo.units.MW)
            )

    # steam required for ASU
    m.fs.ASU_HP_steam_heat = pyo.Var(m.fs.time,
                                     initialize=4, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def ASU_HP_steam_constraint(fs, t):
        return (fs.ASU_HP_steam_heat[t] ==
                0.005381*pyo.units.MJ/pyo.units.mol *
                fs.ASU.O2_outlet.flow_mol[t])

    # HRSG heat applied toward steam cycle
    m.fs.steam_cycle_heat = pyo.Var(m.fs.time,
                                    initialize=300, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def steam_cycle_heat_constraint(fs, t):
        return (fs.steam_cycle_heat[t] == fs.HRSG_heat_duty[t]
                - fs.ASU_HP_steam_heat[t])

    # power generated by steam cycle
    m.fs.steam_cycle_efficiency = pyo.Param(initialize=0.381, mutable=True)
    m.fs.steam_cycle_power = pyo.Var(m.fs.time,
                                     initialize=100, units=pyo.units.MW)
    m.fs.steam_cycle_loss = pyo.Var(m.fs.time,
                                    initialize=200, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def steam_cycle_power_constraint(fs, t):
        return (fs.steam_cycle_power[t] ==
                fs.steam_cycle_heat[t]*fs.steam_cycle_efficiency)

    @m.fs.Constraint(m.fs.time)
    def steam_cycle_loss_constraint(fs, t):
        return (fs.steam_cycle_loss[t] ==
                fs.steam_cycle_heat[t]*(1 - fs.steam_cycle_efficiency))

    # # gross plant power
    # m.fs.gross_power = pyo.Var(m.fs.time, initialize=670, units=pyo.units.MW)

    # @m.fs.Constraint(m.fs.time)
    # def gross_power_constraint(fs, t):
    #     return (fs.gross_power[t] == fs.stack_power_AC[t] +
    #             fs.steam_cycle_power[t])

    # boiler feedwater pump load - steam cycle not modeled, scaled from BB
    m.fs.feedwater_pump_work = pyo.Var(m.fs.time,
                                       initialize=2, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def feedwater_pump_work_constraint(fs, t):
        ref_steam_cycle_power = 262.8*pyo.units.MW
        ref_pump_work = 4.83*pyo.units.MW
        return (fs.feedwater_pump_work[t]*ref_steam_cycle_power ==
                ref_pump_work*fs.steam_cycle_power[t])

    # condensate pump load - steam cycle not modeled, scaled from BB
    m.fs.condensate_pump_work = pyo.Var(m.fs.time,
                                        initialize=0.1, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def condensate_pump_work_constraint(fs, t):
        ref_steam_cycle_power = 262.8*pyo.units.MW
        ref_pump_work = 0.15*pyo.units.MW
        return (fs.condensate_pump_work[t]*ref_steam_cycle_power ==
                ref_pump_work*fs.steam_cycle_power[t])

    # steam turbine auxiliary load - steam cycle not modeled, scaled from BB
    m.fs.steam_turbine_auxiliary = pyo.Var(m.fs.time,
                                           initialize=0.1, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def steam_turbine_auxiliary_constraint(fs, t):
        ref_steam_cycle_power = 262.8*pyo.units.MW
        ref_turbine_aux = 0.2*pyo.units.MW
        return (fs.steam_turbine_auxiliary[t]*ref_steam_cycle_power ==
                ref_turbine_aux*fs.steam_cycle_power[t])

    # miscellaneous BOP - scaled from BB based on NG mass flow
    m.fs.misc_BOP_load = pyo.Var(m.fs.time, initialize=0.5, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def misc_BOP_load_constraint(fs, t):
        NG_flow = pyo.units.convert(
            fs.ng_preheater.tube.properties_in[t].flow_mass,
            pyo.units.lb/pyo.units.hr)
        ref_NG_flow = 148095*pyo.units.lb/pyo.units.hr
        ref_load = 0.396*pyo.units.MW
        return m.fs.misc_BOP_load[t] == ref_load*NG_flow/ref_NG_flow

    # calculate cooling water flowrate
    m.fs.cooling_water_duty = pyo.Var(m.fs.time,
                                      initialize=450, units=pyo.units.MW)
    m.fs.cooling_water_flowrate = pyo.Var(m.fs.time, initialize=3000,
                                          units=pyo.units.lb/pyo.units.s)

    @m.fs.Constraint(m.fs.time)
    def cooling_water_duty_constraint(fs, t):
        return (fs.cooling_water_duty[t] ==
                fs.steam_cycle_loss[t] +
                fs.CPU.heat_duty[t]/1e6*pyo.units.MW +
                7.327*pyo.units.MW +  # 25 MMbtu/hr of additional heat
                pyo.units.convert(
                    -1*fs.intercooler_s1.heat_duty[t] +
                    -1*fs.intercooler_s2.heat_duty[t] +
                    -1*fs.hcmp_ic01.heat_duty[t] +
                    -1*fs.hcmp_ic02.heat_duty[t] +
                    -1*fs.hcmp_ic03.heat_duty[t] +
                    -1*fs.hcmp_ic04.heat_duty[t],
                    pyo.units.MW) +
                pyo.units.convert(
                    -1*fs.condenser.heat_duty[t],
                    pyo.units.MW)
                )

    @m.fs.Constraint(m.fs.time)
    def cooling_water_flowrate_constraint(fs, t):
        heat_capacity = 75.38*pyo.units.J/pyo.units.mol/pyo.units.K
        delta_T = 36*pyo.units.K
        molar_mass = 18*pyo.units.g/pyo.units.mol
        return (fs.cooling_water_flowrate[t] ==
                pyo.units.convert(
                    fs.cooling_water_duty[t]*molar_mass/heat_capacity/delta_T,
                    pyo.units.lb/pyo.units.s))

    # circulating pump work - not modeled, scaled from NGFC pathways study
    m.fs.circulating_pump_work = pyo.Var(m.fs.time,
                                         initialize=1, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def circulating_pump_work_constraint(fs, t):
        ref_flowrate = 18602.6*pyo.units.lb/pyo.units.s
        ref_load = 2.778*pyo.units.MW
        return (fs.circulating_pump_work[t]*ref_flowrate ==
                ref_load*fs.cooling_water_flowrate[t])

    # cooling tower fan load - not modeled, scaled from NGFC pathways study
    m.fs.cooling_tower_load = pyo.Var(m.fs.time,
                                      initialize=1, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def cooling_tower_load_constraint(fs, t):
        ref_flowrate = 18602.6*pyo.units.lb/pyo.units.s
        ref_load = 1.4372*pyo.units.MW
        return (fs.cooling_tower_load[t]*ref_flowrate ==
                ref_load*fs.cooling_water_flowrate[t])

    # auxiliary load of the plant
    m.fs.auxiliary_load = pyo.Var(m.fs.time, initialize=60, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def auxiliary_load_constraint(fs, t):
        return (fs.auxiliary_load[t] == fs.CPU.work[t]/1e6 +
                fs.feedwater_pump_work[t] + fs.condensate_pump_work[t] +
                fs.steam_turbine_auxiliary[t] + fs.misc_BOP_load[t] +
                fs.circulating_pump_work[t] + fs.cooling_tower_load[t] +
                pyo.units.convert(
                   (fs.air_blower.work_mechanical[t]/0.95 +
                    m.fs.aux_boiler_feed_pump.work_mechanical[t]/0.95 +
                    fs.air_compressor_s1.work_mechanical[t]/0.96 +
                    fs.air_compressor_s2.work_mechanical[t]/0.96 +
                    fs.hcmp01.work_mechanical[t]/0.96 +
                    fs.hcmp02.work_mechanical[t]/0.96 +
                    fs.hcmp03.work_mechanical[t]/0.96 +
                    fs.hcmp04.work_mechanical[t]/0.96
                    ), pyo.units.MW))

    # TODO - should the soec have transformer losses i.e. no power sent to grid
    # transformer losses
    m.fs.transformer_losses = pyo.Var(m.fs.time,
                                      initialize=2, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def transformer_losses_constraint(fs, t):
        HV_aux = pyo.units.convert(
            fs.air_compressor_s1.work_mechanical[t]/0.96 +
            fs.air_compressor_s2.work_mechanical[t]/0.96 +
            fs.air_compressor_s2.work_mechanical[t]/0.96 +
            fs.hcmp01.work_mechanical[t]/0.96 +
            fs.hcmp02.work_mechanical[t]/0.96 +
            fs.hcmp03.work_mechanical[t]/0.96 +
            fs.hcmp04.work_mechanical[t]/0.96,
            pyo.units.MW) + fs.CPU.work[t]/1e6
        MV_aux = fs.auxiliary_load[t] - HV_aux
        LV_aux = MV_aux*0.15

        HV_gen_loss = ((fs.steam_cycle_power[t] - fs.auxiliary_load[t])
                       + HV_aux)*.003
        HV_loss = HV_aux*.003
        MV_loss = MV_aux*.005
        LV_loss = LV_aux*.005

        return (fs.transformer_losses[t] ==
                HV_gen_loss + HV_loss + MV_loss + LV_loss)

    # net plant power
    m.fs.net_power = pyo.Var(m.fs.time, initialize=660, units=pyo.units.MW)

    @m.fs.Constraint(m.fs.time)
    def net_power_constraint(fs, t):
        return (fs.net_power[t] == fs.steam_cycle_power[t] -
                fs.soec_power[t] -
                fs.auxiliary_load[t] -
                fs.transformer_losses[t])

    # HHV efficiency
    m.fs.HHV_efficiency = pyo.Var(m.fs.time, initialize=0.6)

    # TODO - HHV efficiency should include H2 produced
    @m.fs.Constraint(m.fs.time)
    def efficiency_rule(fs, t):
        NG_HHV = 908839.23*pyo.units.J/pyo.units.mol
        H2_HHV = 141.7e6*pyo.units.J/pyo.units.kg  # Ref: https://www.engineeringtoolbox.com/fuels-higher-calorific-values-d_169.html
        return (fs.HHV_efficiency[t] == (fs.net_power[t] +
                pyo.units.convert(
                    (H2_HHV * fs.h2_product_rate_mass[t]),
                    pyo.units.MW))
                /
                pyo.units.convert(
                    (NG_HHV * fs.ng_preheater.tube.properties_in[t].flow_mol),
                    pyo.units.MW))

    # CO2 captured in kg/hr
    m.fs.CO2_captured = pyo.Var(m.fs.time, initialize=300,
                                units=pyo.units.kg/pyo.units.hr)

    @m.fs.Constraint(m.fs.time)
    def CO2_captured_constraint(fs, t):
        mass_flow = (fs.CPU.pureco2.flow_mol[t] *
                     fs.CPU.pureco2.mole_frac_comp[t, 'CO2'] *
                     0.04401*pyo.units.kg/pyo.units.mol)
        return fs.CO2_captured[t] == pyo.units.convert(
                            mass_flow, pyo.units.kg/pyo.units.hr)
    # CO2 emissions in kg/hr
    m.fs.CO2_emissions = pyo.Var(m.fs.time, initialize=300,
                                 units=pyo.units.kg/pyo.units.hr)

    @m.fs.Constraint(m.fs.time)
    def CO2_emission_constraint(fs, t):
        mass_flow = (fs.CPU.vent.flow_mol[t] *
                     fs.CPU.vent.mole_frac_comp[t, 'CO2'] *
                     0.04401*pyo.units.kg/pyo.units.mol)
        return fs.CO2_emissions[t] == pyo.units.convert(
                            -1*mass_flow, pyo.units.kg/pyo.units.hr)
    # TODO - include results constraints for water usage/makeup water i.e.
    # TODO - cooling water loss, steam cycle loss, process water for H2 gen


def initialize_results(m):

    variables = [
        m.fs.hydrogen_product_rate,
        m.fs.h2_compressor_power,
        m.fs.soec_power,
        m.fs.HRSG_heat_duty,
        m.fs.ASU_HP_steam_heat,
        m.fs.steam_cycle_heat,
        m.fs.steam_cycle_power,
        m.fs.steam_cycle_loss,
        m.fs.feedwater_pump_work,
        m.fs.condensate_pump_work,
        m.fs.steam_turbine_auxiliary,
        m.fs.misc_BOP_load,
        m.fs.cooling_water_duty,
        m.fs.cooling_water_flowrate,
        m.fs.circulating_pump_work,
        m.fs.cooling_tower_load,
        m.fs.auxiliary_load,
        m.fs.transformer_losses,
        m.fs.net_power,
        m.fs.HHV_efficiency,
        m.fs.CO2_captured,
        m.fs.CO2_emissions]

    constraints = [
        m.fs.hydrogen_product_rate_constraint,
        m.fs.h2_compressor_power_constraint,
        m.fs.soec_power_constraint,
        m.fs.HRSG_heat_duty_constraint,
        m.fs.ASU_HP_steam_constraint,
        m.fs.steam_cycle_heat_constraint,
        m.fs.steam_cycle_power_constraint,
        m.fs.steam_cycle_loss_constraint,
        m.fs.feedwater_pump_work_constraint,
        m.fs.condensate_pump_work_constraint,
        m.fs.steam_turbine_auxiliary_constraint,
        m.fs.misc_BOP_load_constraint,
        m.fs.cooling_water_duty_constraint,
        m.fs.cooling_water_flowrate_constraint,
        m.fs.circulating_pump_work_constraint,
        m.fs.cooling_tower_load_constraint,
        m.fs.auxiliary_load_constraint,
        m.fs.transformer_losses_constraint,
        m.fs.net_power_constraint,
        m.fs.efficiency_rule,
        m.fs.CO2_captured_constraint,
        m.fs.CO2_emission_constraint]

    for v, c in zip(variables, constraints):
        for t in m.fs.time:
            calculate_variable_from_constraint(v[t], c[t])


def set_guess(m):
    # Set guess for tear streams/ports (preheat_split.inlet, soec inlet)
    comp_guess = {  # air_prop is used as assumption as all fuel is combusted
        "O2": 0.2074,
        "H2O": 0.0099,
        "CO2": 0.0003,
        "N2": 0.7732,
        "Ar": 0.0092
    }

    _set_port(
        m.fs.preheat_split.inlet, F=6000, T=700, P=1.04e5,
        comp=comp_guess, fix=True
    )

    # Set guess for temp, pressure and mole frac conditions to initalize soec
    m.fs.soec.fc.temperature[:, 0].fix(1073.15)
    m.fs.soec.fc.pressure[:, 0].fix(1e5)
    m.fs.soec.fc.mole_frac_comp[:, 0, "H2O"].fix(0.90)
    m.fs.soec.fc.mole_frac_comp[:, 0, "H2"].fix(0.10)

    m.fs.soec.ac.temperature[:, 0].fix(1073.15)
    m.fs.soec.ac.pressure[:, 0].fix(1e5)
    m.fs.soec.ac.mole_frac_comp[:, 0, "O2"].fix(0.2074)
    m.fs.soec.ac.mole_frac_comp[:, 0, "H2O"].fix(0.0099)
    m.fs.soec.ac.mole_frac_comp[:, 0, "N2"].fix(0.7732)
    m.fs.soec.ac.mole_frac_comp[:, 0, "Ar"].fix(0.0092)
    m.fs.soec.ac.mole_frac_comp[:, 0, "CO2"].fix(0.0003)


def set_inputs(m):
    # Set combustor outlet temperature and soec steam temperature
    m.fs.cmb_temperature.fix(1300)
    m.fs.soec_steam_temperature.fix(1073.15)

    # Set feed conditions of input fuel and utilities
    # TODO - note that flowrate vars for mxa1.air, air_compressor_s1.inlet,
    # aux_boiler_feed_pump.inlet, and m.fs.ng_preheater.tube_inlet are dof or
    # design specs that will be unfixed at final solve
    air_comp = {
        "O2": 0.2074,
        "H2O": 0.0099,
        "CO2": 0.0003,
        "N2": 0.7732,
        "Ar": 0.0092,
    }
    ng_comp = {
        "CH4": 0.931,
        "C2H6": 0.0320,
        "C3H8": 0.007,
        "C4H10": 0.004,
        "O2": 1e-5,
        "H2O": 1e-5,
        "CO2": 0.01,
        "N2": 0.0160,
        "Ar": 1e-5,
    }
    _set_port(  # TODO - relate NG feed to air feed requirement
        m.fs.air_compressor_s1.inlet, F=5000, T=330, P=1.01325e5,
        comp=air_comp, fix=True
    )
    _set_port(
        m.fs.ng_preheater.tube_inlet, F=380, T=330, P=1.04e5,
        comp=ng_comp, fix=True
    )
    _set_port(  # TODO - replaced mxa1 with air blower inlet
        m.fs.air_blower.inlet, F=3000, T=330, P=1.01325e5,
        comp=air_comp, fix=True
    )
    _set_port(  # TODO - remove alongside mxa1 as recycle is no longer spec'd
        m.fs.mxa1.recycle, F=1e-3, T=1073.15, P=1.04e5,  # Flow set to tiny val
        comp={"O2": 0.2074, "H2O": 0.0099, "CO2": 0.0003,
              "N2": 0.7732, "Ar": 0.0092}, fix=True
    )

    m.fs.aux_boiler_feed_pump.inlet.flow_mol.fix(2000)
    m.fs.aux_boiler_feed_pump.inlet.enth_mol.fix(
        iapws95.htpx(T=310*pyo.units.K, P=101325*pyo.units.Pa)
    )
    m.fs.aux_boiler_feed_pump.inlet.pressure.fix(101325)

    # Set known fixed (assumed) conditions for soec
    # TODO - increased n_cells to meet 600MW usage (equiv prod from sofc stack)
    m.fs.soec.n_cells.fix(400e6)
    m.fs.soec_heat_duty.fix(0)  # going for the thermoneutral point here

    m.fs.soec.fc.mole_frac_comp[:, 0, "H2"].fix(0.10)  # why not unfixed later
    m.fs.soec.fc.flow_mol[:, 0].fix(8e-6)
    m.fs.soec.ac.flow_mol[:, 0].fix(1e-5)


def initialize_plant(m, solver):
    m.fs.preheat_split.initialize()
    iinit.propagate_state(m.fs.o04)
    iinit.propagate_state(m.fs.o05)

    m.fs.air_compressor_s1.initialize()
    iinit.propagate_state(m.fs.STAGE_1_OUT)
    m.fs.intercooler_s1.initialize()
    iinit.propagate_state(m.fs.IC_1_OUT)
    m.fs.air_compressor_s2.initialize()
    iinit.propagate_state(m.fs.STAGE_2_OUT)
    m.fs.intercooler_s2.initialize()
    iinit.propagate_state(m.fs.ba01)
    m.fs.ASU.initialize()
    iinit.propagate_state(m.fs.ba02)
    m.fs.oxygen_preheater.initialize()
    iinit.propagate_state(m.fs.cmb_mix_in)
    m.fs.pre_oxycombustor_translator.initialize()
    iinit.propagate_state(m.fs.ba03)

    m.fs.ng_preheater.initialize()
    iinit.propagate_state(m.fs.bng01)
    m.fs.cmb_mix.initialize()
    iinit.propagate_state(m.fs.bng02)
    m.fs.cmb.initialize()
    iinit.propagate_state(m.fs.cmb_out)

    m.fs.post_oxycombustor_translator.initialize()
    iinit.propagate_state(m.fs.fg01)
    m.fs.soec.initialize()
    iinit.propagate_state(m.fs.h01)
    iinit.propagate_state(m.fs.o01)
    m.fs.spltf1.initialize()
    iinit.propagate_state(m.fs.h02)
    iinit.propagate_state(m.fs.hr01)
    m.fs.splta1.initialize()
    iinit.propagate_state(m.fs.o02)

    m.fs.aux_boiler_feed_pump.initialize()
    iinit.propagate_state(m.fs.s01)
    m.fs.bhx1.initialize()
    iinit.propagate_state(m.fs.s02)
    m.fs.bhx2.initialize()
    iinit.propagate_state(m.fs.s03)

    m.fs.main_steam_split.initialize()
    iinit.propagate_state(m.fs.s10)

    m.fs.mxf1.initialize()
    iinit.propagate_state(m.fs.s12)

    m.fs.air_blower.initialize()
    iinit.propagate_state(m.fs.a01)
    m.fs.air_preheater_1.initialize()
    iinit.propagate_state(m.fs.a02)
    m.fs.air_preheater_2.initialize()
    iinit.propagate_state(m.fs.a03)
    iinit.propagate_state(m.fs.o03)
    m.fs.mxa1.initialize()
    iinit.propagate_state(m.fs.s13)

    # Unfix tear/guess streams
    m.fs.preheat_split.inlet.unfix()

    # Unfix some dof
    # TODO - maybe tie the unfixing of these vars to the add_constraint method
    m.fs.bhx2.outlet.enth_mol.unfix()
    m.fs.air_compressor_s1.inlet.flow_mol.unfix()
    m.fs.ng_preheater.tube_inlet.flow_mol.unfix()
    m.fs.soec.E_cell.unfix()
    # Solve soec model alone before flowsheet solve to check that it converges
    # solver.solve(m.fs.soec, tee=True, symbolic_solver_labels=True)
    # Unfix because cell is solved as thermoneutral (soec_heat_duty.fix(0))
    m.fs.s12_expanded.deactivate()
    m.fs.s13_expanded.deactivate()

    # # TODO - this seems to improve the convergence in some cases
    # iscale.constraint_autoscale_large_jac(m)

    # solver.solve(m, tee=True,
    #              # options={"max_iter":100},
    #              symbolic_solver_labels=True
    #              )

    # Connect soec to BOP - activate relavant arcs, unfix relevant variables
    m.fs.s12_expanded.activate()
    m.fs.s13_expanded.activate()

    m.fs.soec.fc.pressure[:, 0].unfix()
    m.fs.soec.fc.temperature[:, 0].unfix()
    m.fs.soec.fc.mole_frac_comp[:, 0, "H2O"].unfix()
    # TODO - m.fs.soec.fc.mole_frac_comp[:, 0, "H2"].unfix() - why not unfixed
    m.fs.soec.ac.pressure[:, 0].unfix()
    m.fs.soec.ac.temperature[:, 0].unfix()
    m.fs.soec.ac.mole_frac_comp[:, 0, "H2O"].unfix()
    m.fs.soec.ac.mole_frac_comp[:, 0, "O2"].unfix()
    m.fs.soec.ac.mole_frac_comp[:, 0, "N2"].unfix()
    m.fs.soec.ac.mole_frac_comp[:, 0, "Ar"].unfix()
    m.fs.soec.ac.mole_frac_comp[:, 0, "CO2"].unfix()

    m.fs.spltf1.split_fraction[:, "out"].unfix()

    # TODO - these unfixes are related to the fixed soec flow_mol for fc and ac
    # Unfix soec.fc.flow_mol if aux_boiler_feed_pump.inlet.flow_mol is fixed
    m.fs.aux_boiler_feed_pump.inlet.flow_mol.unfix()
    # Unfix soec.ac.flow_mol if mxa1.air.flow_mol is fixed
    m.fs.air_blower.inlet.flow_mol.unfix()

    solver.solve(m, tee=True,
                 options={
                       "max_iter": 200,
                       "tol": 1e-7,
                       "bound_push": 1e-12,
                       "linear_solver": "ma27"
                           },
                 symbolic_solver_labels=True)


def initialize_bop(m, solver):
    copy_port_values(source=m.fs.bhx1.shell_outlet,
                     destination=m.fs.hcmp_ic01.inlet)
    m.fs.hcmp_ic01.initialize()
    iinit.propagate_state(m.fs.h04)
    m.fs.hcmp01.initialize()
    iinit.propagate_state(m.fs.h05)
    m.fs.hcmp_ic02.initialize()
    iinit.propagate_state(m.fs.h06)
    m.fs.hcmp02.initialize()
    iinit.propagate_state(m.fs.h07)
    m.fs.hcmp_ic03.initialize()
    iinit.propagate_state(m.fs.h08)
    m.fs.hcmp03.initialize()
    iinit.propagate_state(m.fs.h09)
    m.fs.hcmp_ic04.initialize()
    iinit.propagate_state(m.fs.h10)
    m.fs.hcmp04.initialize()

    iinit.propagate_state(m.fs.a06)
    m.fs.soec_air_HRSG_1.initialize()
    iinit.propagate_state(m.fs.a07)
    m.fs.soec_air_HRSG_2.initialize()
    iinit.propagate_state(m.fs.fg02)
    m.fs.fluegas_HRSG.initialize()
    iinit.propagate_state(m.fs.fg03)
    m.fs.CPU_translator.initialize()

    iinit.propagate_state(m.fs.fg04)
    m.fs.condenser.initialize()
    iinit.propagate_state(m.fs.fg05)
    m.fs.flash.initialize()

    # TODO - copy_port method used instead of propagate_state because of issue
    # TODO - noticed in tags with propagating flash.vap_outlet to CPU.inlet
    # iinit.propagate_state(m.fs.fg06)
    copy_port_values(source=m.fs.flash.vap_outlet,
                     destination=m.fs.CPU.inlet)
    calculate_variable_from_constraint(m.fs.CPU.inlet.flow_mol[0],
                                       m.fs.CPU_inlet_F[0])
    calculate_variable_from_constraint(m.fs.CPU.inlet.pressure[0],
                                       m.fs.CPU_inlet_P[0])
    m.fs.CPU.initialize()

    # TODO - this seems to improve the convergence in some cases
    iscale.constraint_autoscale_large_jac(m)

    solver.solve(m, tee=True,
                 symbolic_solver_labels=True)


def additional_scaling(m):
    for t, c in m.fs.oxygen_preheater.heat_transfer_equation.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.ng_preheater.heat_transfer_equation.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.bhx1.heat_transfer_equation.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.air_preheater_1.heat_transfer_equation.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.air_preheater_2.heat_transfer_equation.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)

    for t, c in m.fs.mxf1.enthalpy_mixing_equations.items():
        iscale.constraint_scaling_transform(c, 1e-6, overwrite=True)
    for t, c in m.fs.mxa1.enthalpy_mixing_equations.items():
        iscale.constraint_scaling_transform(c, 1e-6, overwrite=True)
    for t, c in m.fs.oxygen_preheater.unit_heat_balance.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.ng_preheater.unit_heat_balance.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.bhx1.unit_heat_balance.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.air_preheater_1.unit_heat_balance.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.air_preheater_2.unit_heat_balance.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)

    for t, c in m.fs.mxf1.fmxpress_eqn.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.mxa1.amxpress_eqn.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.cmb_mix.pressure_eqn.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)

    for t, c in m.fs.cmb_mix.enthalpy_mixing_equations.items():
        iscale.constraint_scaling_transform(c, 1e-6, overwrite=True)
    for t, c in m.fs.cmb.control_volume.enthalpy_balances.items():
        iscale.constraint_scaling_transform(c, 1e-6, overwrite=True)
    for t, c in m.fs.cmb.control_volume.properties_in[t].total_flow_balance.items():
        iscale.constraint_scaling_transform(c, 1e-1, overwrite=True)
    for t, c in m.fs.cmb.control_volume.properties_in[0].component_flow_balances.items():
        iscale.constraint_scaling_transform(c, 1e3, overwrite=True)
    for (t, j), c in m.fs.cmb.control_volume.material_balances.items():
        iscale.constraint_scaling_transform(c, 1e-1, overwrite=True)
    for (t, p, j), c in m.fs.cmb.control_volume.rate_reaction_stoichiometry_constraint.items():
        iscale.constraint_scaling_transform(c, 1e-1, overwrite=True)

    for t, c in m.fs.pre_oxycombustor_translator.pre_oxycombustor_translator_F.items():
        iscale.constraint_scaling_transform(c, 1e-1, overwrite=True)
    for t, c in m.fs.pre_oxycombustor_translator.pre_oxycombustor_translator_P.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)

    for t, c in m.fs.post_oxycombustor_translator.post_oxycombustor_translator_F.items():
        iscale.constraint_scaling_transform(c, 1e-1, overwrite=True)
    for t, c in m.fs.post_oxycombustor_translator.post_oxycombustor_translator_P.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)

    # This scaling term is needed to improve model convergence
    for t, c in m.fs.aux_boiler_feed_pump.eq_work.items():
        iscale.constraint_scaling_transform(c, 1e-10, overwrite=True)

    for (t, n), c in m.fs.soec.fc.flow_mol_eqn.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (t, n, j), c in m.fs.soec.ac.mass_balance_eqn.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (t, j), c in m.fs.mxa1.material_mixing_equations.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (t, n, j), c in m.fs.soec.ac.mass_balance_eqn.items():
        iscale.constraint_scaling_transform(c, 1e-6, overwrite=True)

    for (t, r), v in m.fs.cmb.control_volume.rate_reaction_extent.items():
        iscale.set_scaling_factor(
            m.fs.cmb.control_volume.rate_reaction_extent, 1e-1)
    for (t, p, j), v in m.fs.cmb.control_volume.rate_reaction_generation.items():
        iscale.set_scaling_factor(
            m.fs.cmb.control_volume.rate_reaction_generation, 1e-1)

    iscale.calculate_scaling_factors(m)


def additional_scaling_2(m):
    iscale.set_scaling_factor(m.fs.soec_air_HRSG_1.control_volume.heat, 1e-5)
    iscale.set_scaling_factor(m.fs.soec_air_HRSG_2.control_volume.heat, 1e-5)
    iscale.set_scaling_factor(m.fs.fluegas_HRSG.control_volume.heat, 1e-5)
    iscale.set_scaling_factor(m.fs.condenser.control_volume.heat, 1e-4)
    iscale.set_scaling_factor(m.fs.flash.control_volume.heat, 1e-4)

    for t, c in m.fs.hcmp03.isentropic_pressure.items():
        iscale.constraint_scaling_transform(c, 1e-6, overwrite=True)

    for t, c in m.fs.condenser.control_volume.enthalpy_balances.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for t, c in m.fs.CPU_translator.CPU_translator_F.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for t, c in m.fs.CPU_translator.CPU_translator_P.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)

    for (p, q, j), c in m.fs.CPU_translator.properties_out[t].eq_mole_frac_tdew.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.CPU_translator.properties_out[0.0].equilibrium_constraint.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)

    for (p, q, j), c in m.fs.condenser.control_volume.properties_in[0.0].eq_mole_frac_tdew.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.condenser.control_volume.properties_out[0.0].eq_mole_frac_tdew.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.condenser.control_volume.properties_in[0.0].equilibrium_constraint.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.condenser.control_volume.properties_out[0.0].equilibrium_constraint.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)

    for (p, q, j), c in m.fs.flash.control_volume.properties_in[0.0].eq_mole_frac_tdew.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.flash.control_volume.properties_out[0.0].eq_mole_frac_tdew.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.flash.control_volume.properties_in[0.0].equilibrium_constraint.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for (p, q, j), c in m.fs.flash.control_volume.properties_out[0.0].equilibrium_constraint.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)

    for t, c in m.fs.fluegas_HRSG.control_volume.enthalpy_balances.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)

    for t, c in m.fs.CPU.pureco2_pressure_eq.items():
        iscale.constraint_scaling_transform(c, 1e-5, overwrite=True)
    for t, c in m.fs.CPU.water_pressure_eq.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)
    for t, c in m.fs.CPU.vent_pressure_eq.items():
        iscale.constraint_scaling_transform(c, 1e-3, overwrite=True)

    iscale.calculate_scaling_factors(m)


def get_solver():
    use_idaes_solver_configuration_defaults()
    idaes.cfg.ipopt["options"]["nlp_scaling_method"] = "user-scaling"
    idaes.cfg.ipopt["options"]["tol"] = 1e-7
    # due to a lot of component mole fractions being on their lower bound of 0
    # bound push result in much longer solve times, so set it low.
    idaes.cfg.ipopt["options"]["halt_on_ampl_error"] = 'yes'
    idaes.cfg.ipopt["options"]["bound_push"] = 1e-12
    idaes.cfg.ipopt["options"]["linear_solver"] = "ma57"
    idaes.cfg.ipopt["options"]["max_iter"] = 200
    # idaes.cfg.ipopt["options"]["ma27_pivtol"] = 1e-1
    # idaes.cfg.ipopt["options"]["ma57_pivtol"] = 1e-1
    # idaes.cfg.ipopt["options"]["ma57_pivtolmax"] = 1e-1
    return pyo.SolverFactory("ipopt")


def tag_inputs_opt_vars(m):
    tags = iutil.ModelTagGroup()
    tags["single_cell_h2_side_inlet_flow"] = iutil.ModelTag(
        expr=m.fs.soec.fc.flow_mol[0, 0],
        format_string="{:.3f}",
        display_units=pyo.units.micromol/pyo.units.s,
        doc="Single cell H2 side inlet flow (feed)",
    )
    tags["single_cell_sweep_flow"] = iutil.ModelTag(
        expr=m.fs.soec.ac.flow_mol[0, 0],
        format_string="{:.3f}",
        display_units=pyo.units.micromol/pyo.units.s,
        doc="Single cell O2 side inlet flow (sweep)",
    )
    tags["feed_h2_frac"] = iutil.ModelTag(
        expr=m.fs.soec.fc.mole_frac_comp[0, 0, "H2"],
        format_string="{:.3f}",
        display_units=None,
        doc="H2 side inlet H2 mole frac (from recycle)",
    )
    tags["sweep_o2_frac"] = iutil.ModelTag(
        expr=m.fs.soec.ac.mole_frac_comp[0, 0, "O2"],
        format_string="{:.3f}",
        display_units=None,
        doc="O2 side inlet O2 mole frac (from recycle)",
    )
    tags["single_cell_heat_required"] = iutil.ModelTag(
        expr=m.fs.soec_heat_duty[0],
        format_string="{:.3f}",
        display_units=pyo.units.W,
        doc="Heat duty of a singel SOEC cell",
    )
    tags["combustor_temperature"] = iutil.ModelTag(
        expr=m.fs.cmb.outlet.temperature[0],
        format_string="{:.3f}",
        display_units=pyo.units.K,
        doc="Combustor temperature",
    )
    tags["preheat_fg_split_to_air"] = iutil.ModelTag(
        expr=m.fs.preheat_split.split_fraction[0, "air"],
        format_string="{:.3f}",
        display_units=None,
        doc="Split frac. of soec air to air preheater, rest goes to NG heater",
    )
    tags["n_cells"] = iutil.ModelTag(
        expr=m.fs.soec.n_cells,
        format_string="{:,.0f}",
        display_units=None,
        doc="Number of SOEC cells",
    )
    tags["cell_pressure"] = iutil.ModelTag(
        expr=m.fs.aux_boiler_feed_pump.outlet.pressure[0],
        format_string="{:.3f}",
        display_units=pyo.units.kPa,
        doc="Steam and SOEC pressure",
    )
    # TODO - HEX areas aren't optimization variables as DT is fixed.
    tags["oxygen_preheater_area"] = iutil.ModelTag(
        expr=m.fs.oxygen_preheater.area,
        format_string="{:.3f}",
        display_units=pyo.units.m**2,
        doc="Air preheater area",
    )
    tags["ng_preheater_area"] = iutil.ModelTag(
        expr=m.fs.ng_preheater.area,
        format_string="{:.3f}",
        display_units=pyo.units.m**2,
        doc="NG preheater area",
    )
    tags["bhx1_area"] = iutil.ModelTag(
        expr=m.fs.bhx1.area,
        format_string="{:.3f}",
        display_units=pyo.units.m**2,
        doc="bhx1 area",
    )
    tags["air_preheater_1_area"] = iutil.ModelTag(
        expr=m.fs.air_preheater_1.area,
        format_string="{:.3f}",
        display_units=pyo.units.m**2,
        doc="air_preheater_1 area",
    )
    tags["air_preheater_2_area"] = iutil.ModelTag(
        expr=m.fs.air_preheater_2.area,
        format_string="{:.3f}",
        display_units=pyo.units.m**2,
        doc="air_preheater_2 area",
    )
    m.tag_input = tags
    display_input_tags(m)


def tag_for_pfd_and_tables(m):
    tag_group = iutil.ModelTagGroup()
    stream_states = tables.stream_states_dict(
        tables.arcs_to_stream_dict(
            m.fs,
            descend_into=False,
            additional={
                # TODO - include all inlet streams
                "a08": m.fs.soec_air_HRSG_1.outlet,
                "a09": m.fs.soec_air_HRSG_2.outlet,
                # "fg06": m.fs.flash.vap_outlet,
                "boiler feed water": m.fs.aux_boiler_feed_pump.inlet,
                "H2 product": m.fs.hcmp04.outlet,
                # "CO2 captured": m.fs.CPU.pureco2,
                # "CO2 vented": m.fs.CPU.vent,
            },
        )
    )
    for i, s in stream_states.items():  # create the tags for steam quantities
        tag_group[f"{i}_Fmol"] = iutil.ModelTag(
            expr=s.flow_mol,
            format_string="{:.3f}",
            display_units=pyo.units.kmol/pyo.units.s
        )
        tag_group[f"{i}_Fmass"] = iutil.ModelTag(
            expr=s.flow_mass,
            format_string="{:.3f}",
            display_units=pyo.units.kg/pyo.units.s
        )
        tag_group[f"{i}_P"] = iutil.ModelTag(
            expr=s.pressure,
            format_string="{:.1f}",
            display_units=pyo.units.kPa
        )
        tag_group[f"{i}_T"] = iutil.ModelTag(
            expr=s.temperature,
            format_string="{:.2f}",
            display_units=pyo.units.K
        )
        try:
            tag_group[f"{i}_vf"] = iutil.ModelTag(
                expr=s.phase_frac["Vap"],
                format_string="{:.3f}",
                display_units=None
                )
        except (KeyError, AttributeError):
            pass
        try:
            for c in s.mole_frac_comp:
                tag_group[f"{i}_y{c}"] = iutil.ModelTag(
                    expr=s.mole_frac_comp[c]*100,
                    format_string="{:.3f}",
                    display_units="%"
                )
        except (KeyError, AttributeError):
            pass
        try:
            tag_group[f"{i}_y"] = iutil.ModelTag(
                expr=s.mole_frac_comp,
                format_string="{:.3f}",
                display_units=None
            )
        except (KeyError, AttributeError):
            pass

    tag_group["soec_power"] = iutil.ModelTag(
        expr=m.fs.soec.total_power[0],
        format_string="{:.2f}",
        display_units=pyo.units.MW
    )
    tag_group["soec_n_cells"] = iutil.ModelTag(
        expr=m.fs.soec.n_cells,
        format_string="{:,.0f}",
        display_units=None
    )
    tag_group["E_cell"] = iutil.ModelTag(
        expr=m.fs.soec.E_cell[0],
        format_string="{:.4f}",
        display_units=pyo.units.V
    )

    m.tag_pfd = tag_group


def display_input_tags(m):
    # this is special for this model.  The input tags are not indexed
    print("")
    for key, tag in m.tag_input.items():
        print(key)
        print(f"    {tag.doc}")
        print(f"    display units: {tag._display_units}")
        print(f"    native units: {pyo.units.get_units(tag.expression)}")
        print(f"    value {tag}, fixed: {tag.expression.fixed}")
        print("")


def check_scaling(m):
    import idaes.core.util.scaling as iscale
    jac, nlp = iscale.get_jacobian(m, scaled=True)
    # print("Extreme Jacobian entries:")
    sourceFile = open('extreme_jacobian.txt', 'w')
    for i in iscale.extreme_jacobian_entries(
            jac=jac, nlp=nlp, small=1e-6, large=1e3):
        print(f"    {i[0]:.2e}, [{i[1]}, {i[2]}]", file=sourceFile)
    sourceFile.close()

    # print("Unscaled constraints:")
    sourceFile2 = open('unscaled_constraints.txt', 'w')
    for c in iscale.unscaled_constraints_generator(m):
        print(f"    {c}", file=sourceFile2)
    sourceFile2.close()

    sourceFile3 = open('constraints_with_scale_factor.txt', 'w')
    # print("Scaled constraints by factor:")
    for c, s in iscale.constraints_with_scale_factor_generator(m):
        print(f"    {c}, {s}", file=sourceFile3)
    sourceFile3.close()

    # print("Badly scaled variables:")
    sourceFile4 = open('badly_scaled_var.txt', 'w')
    for v, sv in iscale.badly_scaled_var_generator(
            m, large=1e3, small=1e-4, zero=1e-12):
        print(f"    {v} -- {sv} -- {iscale.get_scaling_factor(v)}",
              file=sourceFile4)
    sourceFile.close()
    print(f"Jacobian Condition Number: {iscale.jacobian_cond(jac=jac):.2e}")


def write_pfd_results(m, filename, infilename=None):
    """
    Write simulation results in a template PFD in svg format and save as
    filename.

    Args:
        filename: (str) file namd for output
        tags: (dict) tag keys and expression values
        tag_format: (dict) tag keys and format string values
        infilename: input file name, if you want to use an alternative diagram

    Returns:
        None
    """
    if infilename is None:
        infilename = os.path.join(this_file_dir(), "rsofc_soec_mode.svg")
    with open(infilename, "r") as f:
        iutil.svg_tag(svg=f, tag_group=m.tag_pfd, outfile=filename)


def get_model(m=None, name="SOEC Module"):
    m = add_flowsheet(m)

    add_properties(m)
    add_asu(m)
    add_preheater(m)
    add_soec_air_side_units(m)
    add_combustor(m)
    add_aux_boiler_steam(m)
    add_soec_unit(m)
    add_more_hx_connections(m)
    add_soec_inlet_mix(m)

    add_design_constraints(m)
    expand_arcs = pyo.TransformationFactory("network.expand_arcs")
    expand_arcs.apply_to(m)
    set_guess(m)
    set_inputs(m)
    solver = get_solver()
    additional_scaling(m)
    iscale.scale_arc_constraints(m)

    initialize_plant(m, solver)
    add_h2_compressor(m)
    add_hrsg_and_cpu(m)
    expand_arcs = pyo.TransformationFactory("network.expand_arcs")
    expand_arcs.apply_to(m)
    additional_scaling_2(m)
    initialize_bop(m, solver)
    check_scaling(m)
    add_result_constraints(m)
    initialize_results(m)
    tag_inputs_opt_vars(m)
    tag_for_pfd_and_tables(m)
    display_input_tags(m)
    return m


if __name__ == "__main__":
    m = get_model()
    # write_pfd_results(m, "soec_init.svg")
