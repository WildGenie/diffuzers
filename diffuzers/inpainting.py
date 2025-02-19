import gc
import json
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import requests
import streamlit as st
import torch
from diffusers import StableDiffusionInpaintPipeline
from loguru import logger
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from streamlit_drawable_canvas import st_canvas

from diffuzers import utils


@dataclass
class Inpainting:
    model: Optional[str] = None
    device: Optional[str] = None
    output_path: Optional[str] = None

    def __str__(self) -> str:
        return f"Inpainting(model={self.model}, device={self.device}, output_path={self.output_path})"

    def __post_init__(self):
        self.pipeline = StableDiffusionInpaintPipeline.from_pretrained(
            self.model,
            torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
        )

        self.pipeline.to(self.device)
        self.pipeline.safety_checker = utils.no_safety_checker
        self._compatible_schedulers = self.pipeline.scheduler.compatibles
        self.scheduler_config = self.pipeline.scheduler.config
        self.compatible_schedulers = {scheduler.__name__: scheduler for scheduler in self._compatible_schedulers}

        if self.device == "mps":
            self.pipeline.enable_attention_slicing()
            # warmup

            def download_image(url):
                response = requests.get(url)
                return Image.open(BytesIO(response.content)).convert("RGB")

            img_url = "https://raw.githubusercontent.com/CompVis/latent-diffusion/main/data/inpainting_examples/overture-creations-5sI6fQgYIuo.png"
            mask_url = "https://raw.githubusercontent.com/CompVis/latent-diffusion/main/data/inpainting_examples/overture-creations-5sI6fQgYIuo_mask.png"

            init_image = download_image(img_url).resize((512, 512))
            mask_image = download_image(mask_url).resize((512, 512))

            prompt = "Face of a yellow cat, high resolution, sitting on a park bench"
            _ = self.pipeline(
                prompt=prompt,
                image=init_image,
                mask_image=mask_image,
                num_inference_steps=2,
            )

    def _set_scheduler(self, scheduler_name):
        scheduler = self.compatible_schedulers[scheduler_name].from_config(self.scheduler_config)
        self.pipeline.scheduler = scheduler

    def generate_image(
        self, prompt, negative_prompt, image, mask, guidance_scale, scheduler, steps, seed, height, width, num_images
    ):
        self._set_scheduler(scheduler)
        logger.info(self.pipeline.scheduler)

        if self.device == "mps":
            generator = torch.manual_seed(seed)
            num_images = 1
        else:
            generator = torch.Generator(device=self.device).manual_seed(seed)

        output_images = self.pipeline(
            prompt=prompt,
            negative_prompt=negative_prompt,
            image=image,
            mask_image=mask,
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            num_images_per_prompt=num_images,
            generator=generator,
            height=height,
            width=width,
        ).images
        metadata = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "guidance_scale": guidance_scale,
            "scheduler": scheduler,
            "steps": steps,
            "seed": seed,
        }
        metadata = json.dumps(metadata)
        _metadata = PngInfo()
        _metadata.add_text("inpainting", metadata)

        utils.save_images(
            images=output_images,
            module="inpainting",
            metadata=metadata,
            output_path=self.output_path,
        )

        torch.cuda.empty_cache()
        gc.collect()
        return output_images, _metadata

    def app(self):
        stroke_color = "#FFF"
        bg_color = "#000"
        col1, col2 = st.columns(2)
        with col1:
            prompt = st.text_area("Prompt", "", key="inpainting_prompt")
        with col2:
            negative_prompt = st.text_area("Negative Prompt", "", key="inpainting_negative_prompt")

        uploaded_file = st.file_uploader(
            "Image:",
            type=["png", "jpg", "jpeg"],
            help="Image size must match model's image size. Usually: 512 or 768",
            key="inpainting_uploaded_file",
        )

        # sidebar options
        available_schedulers = list(self.compatible_schedulers.keys())
        if "EulerAncestralDiscreteScheduler" in available_schedulers:
            available_schedulers.insert(
                0, available_schedulers.pop(available_schedulers.index("EulerAncestralDiscreteScheduler"))
            )
        scheduler = st.sidebar.selectbox("Scheduler", available_schedulers, index=0, key="inpainting_scheduler")
        guidance_scale = st.sidebar.slider("Guidance scale", 1.0, 40.0, 7.5, 0.5, key="inpainting_guidance_scale")
        num_images = st.sidebar.slider("Number of images per prompt", 1, 30, 1, 1, key="inpainting_num_images")
        steps = st.sidebar.slider("Steps", 1, 150, 50, 1, key="inpainting_steps")
        seed_placeholder = st.sidebar.empty()
        seed = seed_placeholder.number_input(
            "Seed", value=42, min_value=1, max_value=999999, step=1, key="inpainting_seed"
        )
        if uploaded_file is not None:
            drawing_mode = st.selectbox("Drawing tool:", ("freedraw", "rect", "circle"), key="inpainting_drawing_mode")
            stroke_width = st.slider("Stroke width: ", 1, 25, 8, key="inpainting_stroke_width")
            pil_image = Image.open(uploaded_file).convert("RGB")
            img_height, img_width = pil_image.size
            canvas_result = st_canvas(
                fill_color="rgb(255, 255, 255)",
                stroke_width=stroke_width,
                stroke_color=stroke_color,
                background_color=bg_color,
                background_image=pil_image,
                update_streamlit=True,
                drawing_mode=drawing_mode,
                height=768,
                width=768,
                key="inpainting_canvas",
            )
            submit = st.button("Generate", key="inpainting_submit")
            if (
                canvas_result.image_data is not None
                and pil_image
                and len(canvas_result.json_data["objects"]) > 0
                and submit
            ):
                mask_npy = canvas_result.image_data[:, :, 3]
                # convert mask npy to PIL image
                mask_pil = Image.fromarray(mask_npy).convert("RGB")
                # resize mask to match image size
                mask_pil = mask_pil.resize((img_width, img_height), resample=Image.LANCZOS)
                with st.spinner("Generating..."):
                    output_images, metadata = self.generate_image(
                        prompt=prompt,
                        negative_prompt=negative_prompt,
                        image=pil_image,
                        mask=mask_pil,
                        guidance_scale=guidance_scale,
                        scheduler=scheduler,
                        steps=steps,
                        seed=seed,
                        height=img_height,
                        width=img_width,
                        num_images=num_images,
                    )

                utils.display_and_download_images(output_images, metadata)
