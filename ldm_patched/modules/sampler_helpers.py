from __future__ import annotations
import uuid
import math
import collections
import ldm_patched.modules.model_management
import ldm_patched.modules.conds
import ldm_patched.modules.utils
import ldm_patched.hooks
import ldm_patched.modules.patcher_extension
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ldm_patched.modules.model_patcher import ModelPatcher
    from ldm_patched.modules.model_base import BaseModel
    from ldm_patched.modules.controlnet import ControlBase

def prepare_mask(noise_mask, shape, device):
    return ldm_patched.modules.utils.reshape_mask(noise_mask, shape).to(device)

def get_models_from_cond(cond, model_type):
    models = []
    for c in cond:
        if model_type in c:
            if isinstance(c[model_type], list):
                models += c[model_type]
            else:
                models += [c[model_type]]
    return models

def get_hooks_from_cond(cond, full_hooks: ldm_patched.hooks.HookGroup):
    # get hooks from conds, and collect cnets so they can be checked for extra_hooks
    cnets: list[ControlBase] = []
    for c in cond:
        if 'hooks' in c:
            for hook in c['hooks'].hooks:
                full_hooks.add(hook)
        if 'control' in c:
            cnets.append(c['control'])

    def get_extra_hooks_from_cnet(cnet: ControlBase, _list: list):
        if cnet.extra_hooks is not None:
            _list.append(cnet.extra_hooks)
        if cnet.previous_controlnet is None:
            return _list
        return get_extra_hooks_from_cnet(cnet.previous_controlnet, _list)

    hooks_list = []
    cnets = set(cnets)
    for base_cnet in cnets:
        get_extra_hooks_from_cnet(base_cnet, hooks_list)
    extra_hooks = ldm_patched.hooks.HookGroup.combine_all_hooks(hooks_list)
    if extra_hooks is not None:
        for hook in extra_hooks.hooks:
            full_hooks.add(hook)

    return full_hooks

def convert_cond(cond):
    out = []
    for c in cond:
        temp = c[1].copy()
        model_conds = temp.get("model_conds", {})
        if c[0] is not None:
            temp["cross_attn"] = c[0]
        temp["model_conds"] = model_conds
        temp["uuid"] = uuid.uuid4()
        out.append(temp)
    return out

def get_additional_models(conds, dtype):
    """loads additional models in conditioning"""
    cnets: list[ControlBase] = []
    gligen = []
    add_models = []

    for k in conds:
        cnets += get_models_from_cond(conds[k], "control")
        gligen += get_models_from_cond(conds[k], "gligen")
        add_models += get_models_from_cond(conds[k], "additional_models")

    control_nets = set(cnets)

    inference_memory = 0
    control_models = []
    for m in control_nets:
        control_models += m.get_models()
        inference_memory += m.inference_memory_requirements(dtype)

    gligen = [x[1] for x in gligen]
    models = control_models + gligen + add_models

    return models, inference_memory

def get_additional_models_from_model_options(model_options: dict[str]=None):
    """loads additional models from registered AddModels hooks"""
    models = []
    if model_options is not None and "registered_hooks" in model_options:
        registered: ldm_patched.hooks.HookGroup = model_options["registered_hooks"]
        for hook in registered.get_type(ldm_patched.hooks.EnumHookType.AdditionalModels):
            hook: ldm_patched.hooks.AdditionalModelsHook
            models.extend(hook.models)
    return models

def cleanup_additional_models(models):
    """cleanup additional models that were loaded"""
    for m in models:
        if hasattr(m, 'cleanup'):
            m.cleanup()

def estimate_memory(model, noise_shape, conds):
    cond_shapes = collections.defaultdict(list)
    cond_shapes_min = {}
    for _, cs in conds.items():
        for cond in cs:
            for k, v in model.model.extra_conds_shapes(**cond).items():
                cond_shapes[k].append(v)
                if cond_shapes_min.get(k, None) is None:
                    cond_shapes_min[k] = [v]
                elif math.prod(v) > math.prod(cond_shapes_min[k][0]):
                    cond_shapes_min[k] = [v]

    memory_required = model.model.memory_required([noise_shape[0] * 2] + list(noise_shape[1:]), cond_shapes=cond_shapes)
    minimum_memory_required = model.model.memory_required([noise_shape[0]] + list(noise_shape[1:]), cond_shapes=cond_shapes_min)
    return memory_required, minimum_memory_required


def prepare_sampling(model: ModelPatcher, noise_shape, conds, model_options=None):
    executor = ldm_patched.modules.patcher_extension.WrapperExecutor.new_executor(
        _prepare_sampling,
        ldm_patched.modules.patcher_extension.get_all_wrappers(ldm_patched.modules.patcher_extension.WrappersMP.PREPARE_SAMPLING, model_options, is_model_options=True)
    )
    return executor.execute(model, noise_shape, conds, model_options=model_options)

def _prepare_sampling(model: ModelPatcher, noise_shape, conds, model_options=None):
    real_model: BaseModel = None
    models, inference_memory = get_additional_models(conds, model.model_dtype())
    models += get_additional_models_from_model_options(model_options)
    models += model.get_nested_additional_models()  # TODO: does this require inference_memory update?
    memory_required, minimum_memory_required = estimate_memory(model, noise_shape, conds)
    ldm_patched.modules.model_management.load_models_gpu([model] + models, memory_required=memory_required + inference_memory, minimum_memory_required=minimum_memory_required + inference_memory)
    real_model = model.model

    return real_model, conds, models

def cleanup_models(conds, models):
    cleanup_additional_models(models)

    control_cleanup = []
    for k in conds:
        control_cleanup += get_models_from_cond(conds[k], "control")

    cleanup_additional_models(set(control_cleanup))

def prepare_model_patcher(model: 'ModelPatcher', conds, model_options: dict):
    '''
    Registers hooks from conds.
    '''
    # check for hooks in conds - if not registered, see if can be applied
    hooks = ldm_patched.hooks.HookGroup()
    for k in conds:
        get_hooks_from_cond(conds[k], hooks)
    # add wrappers and callbacks from ModelPatcher to transformer_options
    model_options["transformer_options"]["wrappers"] = ldm_patched.modules.patcher_extension.copy_nested_dicts(model.wrappers)
    model_options["transformer_options"]["callbacks"] = ldm_patched.modules.patcher_extension.copy_nested_dicts(model.callbacks)
    # begin registering hooks
    registered = ldm_patched.hooks.HookGroup()
    target_dict = ldm_patched.hooks.create_target_dict(ldm_patched.hooks.EnumWeightTarget.Model)
    # handle all TransformerOptionsHooks
    for hook in hooks.get_type(ldm_patched.hooks.EnumHookType.TransformerOptions):
        hook: ldm_patched.hooks.TransformerOptionsHook
        hook.add_hook_patches(model, model_options, target_dict, registered)
    # handle all AddModelsHooks
    for hook in hooks.get_type(ldm_patched.hooks.EnumHookType.AdditionalModels):
        hook: ldm_patched.hooks.AdditionalModelsHook
        hook.add_hook_patches(model, model_options, target_dict, registered)
    # handle all WeightHooks by registering on ModelPatcher
    model.register_all_hook_patches(hooks, target_dict, model_options, registered)
    # add registered_hooks onto model_options for further reference
    if len(registered) > 0:
        model_options["registered_hooks"] = registered
    # merge original wrappers and callbacks with hooked wrappers and callbacks
    to_load_options: dict[str] = model_options.setdefault("to_load_options", {})
    for wc_name in ["wrappers", "callbacks"]:
        ldm_patched.modules.patcher_extension.merge_nested_dicts(to_load_options.setdefault(wc_name, {}), model_options["transformer_options"][wc_name],
                                                    copy_dict1=False)
    return to_load_options
