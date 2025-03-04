import numpy as np
import torch

from comfy import supported_models_base
from comfy.latent_formats import SD15
from comfy.model_base import BaseModel

from coreml_suite.controlnet import extract_residual_kwargs, chunk_control
from coreml_suite.latents import chunk_batch, merge_chunks


def get_model_config():
    # TODO: This is a dummy model config, but it should be enough to
    #  get the model to load - implement a proper model config
    model_config = supported_models_base.BASE({})
    model_config.latent_format = SD15()
    model_config.unet_config = {
        "disable_unet_model_creation": True,
        "num_res_blocks": 2,
        "attention_resolutions": [1, 2, 4],
        "channel_mult": [1, 2, 4, 4],
        "transformer_depth": [1, 1, 1, 0],
    }
    return model_config


class CoreMLModelWrapper(BaseModel):
    def __init__(self, model_config, coreml_model):
        super().__init__(model_config)
        self.diffusion_model = coreml_model

    def apply_model(
        self,
        x,
        t,
        c_concat=None,
        c_crossattn=None,
        c_adm=None,
        control=None,
        transformer_options={},
        **kwargs,
    ):
        chunked_in = self.chunk_inputs(
            x, t, c_crossattn, control, kwargs.get("timestep_cond")
        )
        chunked_out = [
            self._apply_model(x, t, c_crossattn, control, ts_cond)
            for x, t, c_crossattn, control, ts_cond in zip(*chunked_in)
        ]
        merged_out = merge_chunks(chunked_out, x.shape)
        return merged_out

    def _apply_model(self, x, t, c_crossattn, control=None, ts_cond=None):
        model_input_kwargs = self.prepare_inputs(x, t, c_crossattn, control, ts_cond)

        np_out = self.diffusion_model(**model_input_kwargs)["noise_pred"]
        return torch.from_numpy(np_out).to(x.device)

    def get_dtype(self):
        # Hardcoding torch-compatible dtype (used for memory allocation)
        return torch.float16

    def prepare_inputs(self, x, t, c_crossattn, control, ts_cond=None):
        sample = x.cpu().numpy().astype(np.float16)

        context = c_crossattn.cpu().numpy().astype(np.float16)
        context = context.transpose(0, 2, 1)[:, :, None, :]

        t = t.cpu().numpy().astype(np.float16)

        model_input_kwargs = {
            "sample": sample,
            "encoder_hidden_states": context,
            "timestep": t,
        }
        residual_kwargs = extract_residual_kwargs(self.diffusion_model, control)
        model_input_kwargs |= residual_kwargs

        if ts_cond is not None:
            model_input_kwargs["timestep_cond"] = (
                ts_cond.cpu().numpy().astype(np.float16)
            )

        return model_input_kwargs

    def chunk_inputs(self, x, t, c_crossattn, control, ts_cond=None):
        sample_shape = self.expected_inputs["sample"]["shape"]
        timestep_shape = self.expected_inputs["timestep"]["shape"]
        hidden_shape = self.expected_inputs["encoder_hidden_states"]["shape"]
        context_shape = (hidden_shape[0], hidden_shape[3], hidden_shape[1])

        chunked_x = chunk_batch(x, sample_shape)
        ts = list(torch.full((len(chunked_x), timestep_shape[0]), t[0]))
        chunked_context = chunk_batch(c_crossattn, context_shape)

        chunked_control = [None] * len(chunked_x)
        if control is not None:
            chunked_control = chunk_control(control, sample_shape[0])

        chunked_ts_cond = [None] * len(chunked_x)
        if ts_cond is not None:
            ts_cond_shape = self.expected_inputs["timestep_cond"]["shape"]
            chunked_ts_cond = chunk_batch(ts_cond, ts_cond_shape)

        return chunked_x, ts, chunked_context, chunked_control, chunked_ts_cond

    @property
    def expected_inputs(self):
        return self.diffusion_model.expected_inputs

    def __call__(self, latents, ts, encoder_hidden_states, **kwargs):
        return (
            self.apply_model(latents, ts, c_crossattn=encoder_hidden_states, **kwargs),
        )


class CoreMLModelWrapperLCM(CoreMLModelWrapper):
    def __init__(self, model_config, coreml_model):
        super().__init__(model_config, coreml_model)
        self.config = None

    def __call__(self, latents, ts, encoder_hidden_states, **kwargs):
        return (
            self.apply_model(latents, ts, c_crossattn=encoder_hidden_states, **kwargs),
        )
