"""Repair MOVA's direct helper calls with FSDP2, without importing SGLang.

Run with the inference environment's Python, or pass --site-packages explicitly.
The original file is backed up once; repeated execution is safe.
"""
import argparse
import ast
import shutil
import sysconfig
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-packages', type=Path,
                        default=Path(sysconfig.get_paths()['purelib']))
    args = parser.parse_args()
    target = args.site_packages / 'sglang/multimodal_gen/runtime/pipelines_core/stages/model_specific_stages/mova.py'
    original = target.read_text()
    backup = target.with_suffix('.py.before-studio-fsdp')
    marker = '    def _inference_single_step_impl('
    if marker in original:
        if 'register_fsdp_forward_method' in original:
            print(f'Already patched: {target}')
            return
        # Upgrade the initial manual-unshard fix to registered root hooks.
        if not backup.exists():
            raise SystemExit('Original source backup is missing; no files changed.')
        original = backup.read_text()
    anchor = '    def inference_single_step(\n'
    import_anchor = 'import torch.nn as nn\n'
    if original.count(anchor) != 1 or original.count(import_anchor) != 1:
        raise SystemExit('Unsupported MOVA source layout; no files changed.')
    wrapper = '''    @staticmethod
    def _run_mova_fsdp_root(model, callback):
        if not isinstance(model, FSDPModule):
            return callback()
        if not hasattr(model, "_mova_stage_forward"):
            # Enter the root before its blocks, so FSDP initializes one shared
            # communication context and releases its gather buffers afterward.
            model._mova_stage_forward = lambda run: run()
            register_fsdp_forward_method(model, "_mova_stage_forward")
        return model._mova_stage_forward(callback)

    def inference_single_step(self, visual_dit, *args, **kwargs):
        # MOVA calls embeddings, blocks and heads directly, bypassing each
        # DiT's normal forward. Registered entrypoints run the root FSDP hooks
        # while the individual block groups still gather/reshard per layer.
        return self._run_mova_fsdp_root(
            visual_dit,
            lambda: self._run_mova_fsdp_root(
                self.audio_dit,
                lambda: self._inference_single_step_impl(visual_dit, *args, **kwargs),
            ),
        )

    def _inference_single_step_impl(
'''
    patched = original.replace(anchor, wrapper).replace(
        import_anchor, import_anchor + 'from torch.distributed.fsdp import FSDPModule, register_fsdp_forward_method\n')
    ast.parse(patched)
    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(patched)
    print(f'Patched: {target}; original: {backup}')


if __name__ == '__main__':
    main()
