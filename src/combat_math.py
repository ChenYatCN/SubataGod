from typing import List, Coroutine, Any
import math
from wizwalker import Client
from wizwalker.combat import CombatMember
from wizwalker.memory.memory_objects.spell_effect import DynamicSpellEffect, SpellEffects, SpellEffect
from src.combat_objects import get_total_effects, id_to_member, school_list_ids, opposite_school_ids
from src.combat_utils import add_universal_stat
from dataclasses import dataclass


@dataclass
class EffectAttributes:
    """A non-async cache of spell effect attributes used in dmg calculations"""
    effect_param: int
    effect_type: SpellEffects
    damage_type: int
    spell_template_id: int
    enchantment_spell_template_id: int

    @classmethod
    async def from_spell_effect(cls, effect: SpellEffect):
        return cls(
            await effect.effect_param(),
            await effect.effect_type(),
            await effect.damage_type(),
            await effect.spell_template_id(),
            await effect.enchantment_spell_template_id()
        )


async def real_stat(stat_func: Coroutine[Any, Any, List[float]], uni_func: Coroutine[Any, Any, float]) -> List[float]:
    # Handles adding two stat reading coroutines
    base_stats = await stat_func()
    uni_stat = await uni_func()

    return add_universal_stat(base_stats, uni_stat)


def curve_stat(stat: float, l: float, k0: float, n0: float) -> float:
    # Curves a stat in the same way the game does with resist and damage values past a certain intersection (starting point of the limit)
    if stat > (k0 + n0) / 100:
        limit = l * 100

        # Calculate k, thank you charlied134 and Major
        if k0 != 0:
            k = math.log(limit / (limit - k0)) / k0
        else:
            k = 1 / limit

        # Calculate n, thank you charlied134 and Major
        n = math.log(1 - (k0 + n0) / limit) + k * (k0 + n0)

        stat = l - l * math.e ** (-1 * k * (stat * 100) + n)

    return stat


async def curve_damage(client: Client, member: CombatMember, damage: float) -> float:
    if await member.is_player():
        l = await client.duel.damage_limit()
        k0 = await client.duel.d_k0()
        n0 = await client.duel.d_n0()

        return curve_stat(damage, l, k0, n0)

    return damage


async def curve_resist(client: Client, member: CombatMember, resist: float) -> float:
    if await member.is_player():
        l = await client.duel.resist_limit()
        k0 = await client.duel.r_k0()
        n0 = await client.duel.r_n0()

        return curve_stat(resist, l, k0, n0)

    return resist


def spell_effect_stacking_id(spell_template_id: int, enchantment_spell_template_id: int) -> tuple:
    """
    Calculate a spell effect stacking ID by combining spell_template_id and enchantment_spell_template_id.
    If two stacking IDs match, the spell effects do not stack.
     - Aegis and Indemity don't stack with un-enchanted versions of the SpellEffect
    """
    enchantments_not_stackable_with_unenchanted = {
        655113637,  # Indemity
        85300353,  # Aegis
        # TODO: Idemnity (Item Card)
        # TODO: Aegis (Item Card)
    }

    if enchantment_spell_template_id in enchantments_not_stackable_with_unenchanted:
        enchantment_spell_template_id = 0  # For stacking purposes, these are the same as unenchanted.
    return (spell_template_id, enchantment_spell_template_id)


async def base_damage_calculation_from_id(client: Client, members: List[CombatMember], caster_id: int, target_id: int, damage: float, damage_type: int, global_effect: DynamicSpellEffect = None, force_crit: bool = False) -> float:
    # Calculates damage from given base damage value, and is the basis for both exact and damage potential calculation. Works based off of IDs.

    # Get base objects from ID arguments
    caster = await id_to_member(caster_id, members)
    target = await id_to_member(target_id, members)

    # Caster-specific objects
    caster_stats = await caster.get_stats()
    # Charms use FIFO (queue behavior) in game, but the first applied blades show up at the bottom of this list.
    caster_effects: List[DynamicSpellEffect] = await get_total_effects(caster_id, members)
    caster_effects.reverse()

    # Target-specific objects
    target_stats = await target.get_stats()
    # Traps/Shields use LIFO (stack behavior) in game.
    target_effects: List[DynamicSpellEffect] = await get_total_effects(target_id, members)

    # Global effects
    caster_effects.append(global_effect)
    target_effects.append(global_effect)

    # Caster Stats
    caster_damages = await real_stat(caster_stats.dmg_bonus_percent, caster_stats.dmg_bonus_percent_all)
    caster_flat_damages = await real_stat(caster_stats.dmg_bonus_flat, caster_stats.dmg_bonus_flat_all)
    caster_crits = await real_stat(caster_stats.critical_hit_rating_by_school, caster_stats.critical_hit_rating_all)
    caster_pierces = await real_stat(caster_stats.ap_bonus_percent, caster_stats.ap_bonus_percent_all)
    caster_level = await caster.level()

    # Target Stats
    target_resistances = await real_stat(target_stats.dmg_reduce_percent, target_stats.dmg_reduce_percent_all)
    target_flat_resistances = await real_stat(target_stats.dmg_reduce_flat, target_stats.dmg_reduce_flat_all)
    target_blocks = await real_stat(target_stats.block_rating_by_school, target_stats.block_rating_all)

    # Break up caster hanging effect objects
    caster_effect_atrs: List[EffectAttributes] = []
    for effect in caster_effects:
        if effect:
            caster_effect_atrs.append(await EffectAttributes.from_spell_effect(effect))

    target_effect_atrs: List[EffectAttributes] = []
    for effect in target_effects:
        if effect:
            target_effect_atrs.append(await EffectAttributes.from_spell_effect(effect))

    initial_damage_type = damage_type
    initial_damage_type_index = school_list_ids[damage_type]

    # Relevant caster stats for the damage type
    caster_damage = caster_damages[initial_damage_type_index]
    caster_flat_damages = caster_flat_damages[initial_damage_type_index]
    caster_crit = caster_crits[initial_damage_type_index]
    caster_pierce = caster_pierces[initial_damage_type_index]

    # Curve damage stats
    curved_caster_damage = await curve_damage(client, caster, caster_damage)
    curved_caster_damage += 1

    # Applying curved damage and flat damage
    damage *= curved_caster_damage
    damage += caster_flat_damages

    # outgoing hanging effects (caster)
    seen_caster_effect_stacking_ids = set()
    for effect_atr in caster_effect_atrs:
        stacking_id = spell_effect_stacking_id(effect_atr.spell_template_id, effect_atr.enchantment_spell_template_id)
        # only consider effects that matches the school or are universal
        if stacking_id not in seen_caster_effect_stacking_ids \
                and (effect_atr.damage_type == damage_type or effect_atr.damage_type == 80289):
            seen_caster_effect_stacking_ids.add(stacking_id)
            match effect_atr.effect_type:
                case SpellEffects.modify_outgoing_damage:
                    damage *= (effect_atr.effect_param / 100) + 1

                case SpellEffects.modify_outgoing_damage_flat:
                    damage += effect_atr.effect_param

                case SpellEffects.modify_outgoing_armor_piercing:
                    caster_pierce += effect_atr.effect_param

                case SpellEffects.modify_outgoing_damage_type:
                    damage_type = effect_atr.effect_param

                case _:
                    pass

    # incoming hanging effects (target)
    seen_target_effect_stacking_ids = set()
    for effect_atr in target_effect_atrs:
        stacking_id = spell_effect_stacking_id(effect_atr.spell_template_id, effect_atr.enchantment_spell_template_id)
        if stacking_id not in seen_target_effect_stacking_ids \
                and (effect_atr.damage_type == damage_type or effect_atr.damage_type == 80289):
            seen_target_effect_stacking_ids.add(stacking_id)
            match effect_atr.effect_type:
                # traps/shields, and pierce handling
                case SpellEffects.modify_incoming_damage:
                    ward_param = effect_atr.effect_param
                    if ward_param < 0:
                        ward_param += caster_pierce
                        caster_pierce += effect_atr.effect_param
                        if ward_param > 0:
                            ward_param = 0
                        if caster_pierce < 0:
                            caster_pierce = 0
                    damage *= (ward_param / 100) + 1

                case SpellEffects.intercept:
                    damage *= (effect_atr.effect_param / 100) + 1

                case SpellEffects.modify_incoming_damage_flat:
                    damage += effect_atr.effect_param

                case SpellEffects.absorb_damage:
                    damage += effect_atr.effect_param

                case SpellEffects.modify_incoming_armor_piercing:
                    caster_pierce += effect_atr.effect_param

                # prism handling (final damage type is the effect param)
                case SpellEffects.modify_incoming_damage_type:
                    damage_type = effect_atr.effect_param

                case _:
                    pass

    final_damage_type = damage_type
    final_damage_type_index = school_list_ids[final_damage_type]

    # Relevant target stats for the final damage type
    target_resist = target_resistances[final_damage_type_index]
    target_flat_resist = target_flat_resistances[final_damage_type_index]
    target_block = target_blocks[final_damage_type_index]

    # Curve the resist stat.
    curved_target_resist = await curve_resist(client, target, target_resist)

    # calculates critical multiplier and chance
    # This assumes that caster crit uses the initial damage school, but target block applies to the final damage school.
    if caster_crit > 0:
        if caster_level > 100:
            caster_level = 100

        crit_damage_multiplier = (2 - (target_block / ((caster_crit / 3) + target_block)))
        client_school_critical = (0.03 * caster_level * caster_crit)
        mob_block = (3 * caster_crit + target_block)
        crit_chance = client_school_critical / mob_block

        # applying the crit multiplier if the chance is above a certain threshold
        # TODO: Express both the crit & non-crit values, along with the crit percentage.
        if (crit_chance >= 0.85 and force_crit is None) or force_crit:
            damage *= crit_damage_multiplier

    # Apply flat resist
    damage -= target_flat_resist
    damage = abs(damage)

    # apply resist, accounting for pierce and potential boost
    if curved_target_resist > 0:
        curved_target_resist -= caster_pierce
        if curved_target_resist <= 0:
            curved_target_resist = 1
        else:
            curved_target_resist = 1 - curved_target_resist
    else:
        curved_target_resist = abs(curved_target_resist) + 1

    damage *= curved_target_resist

    return damage
