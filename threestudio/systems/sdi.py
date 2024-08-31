from dataclasses import dataclass, field

import torch

import threestudio
from threestudio.systems.base import BaseLift3DSystem
from threestudio.utils.ops import binary_cross_entropy, dot
from threestudio.utils.typing import *
import torch.nn.functional as F


@threestudio.register("sdi-system")
class ScoreDistillationViaInversion(BaseLift3DSystem):
    @dataclass
    class Config(BaseLift3DSystem.Config):
        pass

    cfg: Config

    def configure(self):
        # create geometry, material, background, renderer
        super().configure()

    def forward(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        render_out = self.renderer(**batch)
        return {
            **render_out,
        }

    def on_fit_start(self) -> None:
        super().on_fit_start()
        # only used in training
        self.prompt_processor = threestudio.find(self.cfg.prompt_processor_type)(
            self.cfg.prompt_processor
        )
        self.guidance = threestudio.find(self.cfg.guidance_type)(self.cfg.guidance)

    def training_step(self, batch, batch_idx):
        out = self(batch)
        prompt_utils = self.prompt_processor()
        guidance_out = self.guidance(
            out["comp_rgb"], prompt_utils, **batch, rgb_as_latents=False
        )

        loss = 0.0

        for name, value in guidance_out.items():
            if not (type(value) is torch.Tensor and value.numel() > 1):
                self.log(f"train/{name}", value)
            if name.startswith("loss_"):
                loss += value * self.C(self.cfg.loss[name.replace("loss_", "lambda_")])

        if self.C(self.cfg.loss.lambda_orient) > 0:
            if "normal" not in out:
                raise ValueError(
                    "Normal is required for orientation loss, no normal is found in the output."
                )
            loss_orient = (
                out["weights"].detach()
                * dot(out["normal"], out["t_dirs"]).clamp_min(0.0) ** 2
            ).sum() / (out["opacity"] > 0).sum()
            self.log("train/loss_orient", loss_orient)
            loss += loss_orient * self.C(self.cfg.loss.lambda_orient)

        loss_sparsity_initial = (out["opacity"] ** 2 + 0.01)
        loss_sparsity_sqrt = loss_sparsity_initial.sqrt()
        loss_sparsity = F.relu(loss_sparsity_sqrt.mean())
        self.log("train/loss_sparsity", loss_sparsity)
        loss += loss_sparsity * self.C(self.cfg.loss.lambda_sparsity)

        opacity_clamped = out["opacity"].clamp(1.0e-3, 1.0 - 1.0e-3)
        loss_opaque = binary_cross_entropy(opacity_clamped, opacity_clamped)
        self.log("train/loss_opaque", loss_opaque)
        loss += loss_opaque * self.C(self.cfg.loss.lambda_opaque)

        # z-variance loss proposed in HiFA: https://hifa-team.github.io/HiFA-site/
        if "z_variance" in out and "lambda_z_variance" in self.cfg.loss:
            loss_z_variance = out["z_variance"][out["opacity"] > 0.5].mean()
            self.log("train/loss_z_variance", loss_z_variance)
            loss += loss_z_variance * self.C(self.cfg.loss.lambda_z_variance)

        for name, value in self.cfg.loss.items():
            self.log(f"train_params/{name}", self.C(value))

        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        out = self(batch)
        
        with torch.no_grad():
            pred_x0_latent, noisy_latent = self.guidance(
                out["comp_rgb"], self.prompt_processor(), **batch, rgb_as_latents=False, test_call=True
            )
        
        # self.save_image_grid(
        #     f"it{self.true_global_step}-{batch['index'][0]}.png",
        #     [
        #         {
        #             "type": "rgb",
        #             "img": out["comp_rgb"][0],
        #             "kwargs": {"data_format": "HWC"},
        #         },
        #     ]
        #     + (
        #         [
        #             {
        #                 "type": "rgb",
        #                 "img": out["comp_normal"][0],
        #                 "kwargs": {"data_format": "HWC", "data_range": (0, 1)},
        #             }
        #         ]
        #         if "comp_normal" in out
        #         else []
        #     )
        #     + [
        #         {
        #             "type": "grayscale",
        #             "img": out["opacity"][0, :, :, 0],
        #             "kwargs": {"cmap": None, "data_range": (0, 1)},
        #         },
        #     ],
        #     name="validation_step",
        #     step=self.true_global_step,
        # )
        
        self.save_image_grid(
            f"it{self.true_global_step}-{batch['index'][0]}.png",
            [
                {
                    "type": "rgb",
                    "img": out["comp_rgb"][0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + (
                [
                    {
                        "type": "rgb",
                        "img": out["comp_normal"][0],
                        "kwargs": {"data_format": "HWC", "data_range": (0, 1)},
                    }
                ]
                if "comp_normal" in out
                else []
            )
            + (
                [
                    {
                        "type": "grayscale",
                        "img": out["depth_d"][0, :, :, 0],
                        "kwargs": {"cmap": None, "data_range": (0, 1)},
                    }
                ]
                if "depth_d" in out
                else []
            )
            + (
                [
                    {
                        "type": "rgb",
                        "img": add_channel_to_image( ( get_img_eigenvalues(out["depth_d"]) * out["opacity"] ) )[0],
                        "kwargs": {"data_format": "HWC", "data_range": (-.1, .1)},
                    }
                ]
                if "depth_d" in out
                else []
            )
            + [
                {
                    "type": "grayscale",
                    "img": out["opacity"][0, :, :, 0],
                    "kwargs": {"cmap": None, "data_range": (0, 1)},
                },
            ]
            # + [
            #     {
            #         "type": "grayscale",
            #         "img": rescaled_nerf_depth[0, :, :, 0],
            #         "kwargs": {"cmap": None, "data_range": (0, 1)},
            #     },
            # ]
            + [
                {
                    "type": "rgb",
                    "img": self.guidance.decode_latents(noisy_latent)[0].permute(1, 2, 0),
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + [
                {
                    "type": "rgb",
                    "img": self.guidance.decode_latents(pred_x0_latent)[0].permute(1, 2, 0),
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + [
                {
                    "type": "rgb",
                    "img": self.guidance.decode_latents(pred_x0_latent)[0].permute(1, 2, 0) - out["comp_rgb"][0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + (
                [
                    {
                        "type": "grayscale",
                        "img": predicted_depth[0, :, :, 0],
                        "kwargs": {"cmap": None, "data_range": (0, 1)},
                    }
                ]
                if "lambda_depth" in self.cfg.loss
                else []
            )
            ,
            name="validation_step", 
            step=self.true_global_step,
        )


    def on_validation_epoch_end(self):
        pass

    def test_step(self, batch, batch_idx):
        out = self(batch)
        self.save_image_grid(
            f"it{self.true_global_step}-test/{batch['index'][0]}.png",
            [
                {
                    "type": "rgb",
                    "img": out["comp_rgb"][0],
                    "kwargs": {"data_format": "HWC"},
                },
            ]
            + (
                [
                    {
                        "type": "rgb",
                        "img": out["comp_normal"][0],
                        "kwargs": {"data_format": "HWC", "data_range": (0, 1)},
                    }
                ]
                if "comp_normal" in out
                else []
            )
            + [
                {
                    "type": "grayscale",
                    "img": out["opacity"][0, :, :, 0],
                    "kwargs": {"cmap": None, "data_range": (0, 1)},
                },
            ],
            name="test_step",
            step=self.true_global_step,
        )

    def on_test_epoch_end(self):
        self.save_img_sequence(
            f"it{self.true_global_step}-test",
            f"it{self.true_global_step}-test",
            "(\d+)\.png",
            save_format="mp4",
            fps=30,
            name="test",
            step=self.true_global_step,
        )