import pypdfium2
from marker.convert import convert_single_pdf
import logging
import fitz  # PyMuPDF
import io
import tempfile
import torch
import torch.multiprocessing as mp
import logging
import os
import math
from multiprocessing import Pool, cpu_count
from PIL import Image
from typing import List, Tuple, Any, Dict
from . import md_processor
import re
from marker.models import load_all_models
import tempfile
from marker.settings import settings
from ...type import FileDescriptor
from .processor import Processor, ProcessorConfig

from ...process.utils import (
    clean_text,
    create_sample,
    create_sample_list,
    merge_split_with_full_page_indexer,
    create_sample_list_already_saved_images
)

BATCH_SIZE = 1

logger = logging.getLogger(__name__)


class PDFProcessor(Processor):
    def __init__(self, files, config=None):
        super().__init__(files, config=config or ProcessorConfig())
        self.ocr_models = {device: None for device in range(torch.cuda.device_count())}

    @classmethod
    def accepts(cls, file: FileDescriptor) -> bool:
        return file.file_extension.lower() == ".pdf"

    def require_gpu(self) -> Tuple[bool, bool]:
        return True, False

    def load_models(self, device=None):
        """Load the OCR models, optionally on a specific device."""
        if device is None:
            device = torch.cuda.current_device()
            device = torch.device(device)
        device_index = device.index or 0

        def custom_get(dict, key, default, zero_value=0, value_if=cpu_count()):
            # Get a value from a dictionary, with a default value if the key is not present, and a different value if the value is zero
            # Especially useful for settings that cannot be set with a number in a config file  (e.g. number of CPU workers)
            val = dict.get(key, default)
            if val == zero_value:
                return value_if
            return val

        if self.ocr_models[device_index] is None:
            model_lst = load_all_models(device, dtype=torch.float16)
            self.ocr_models = {}
            self.ocr_models[device_index] = model_lst
            logger.info(f"OCR models loaded successfully on device {device}.")

            settings.PDFTEXT_CPU_WORKERS = custom_get(
                self.config.custom_config, "PDFTEXT_CPU_WORKERS", 0)
            settings.DETECTOR_BATCH_SIZE = custom_get(
                self.config.custom_config, "DETECTOR_BATCH_SIZE", 20)
            settings.DETECTOR_POSTPROCESSING_CPU_WORKERS = custom_get(
                self.config.custom_config, "DETECTOR_POSTPROCESSING_CPU_WORKERS", 0)
            settings.RECOGNITION_BATCH_SIZE = custom_get(
                self.config.custom_config, "RECOGNITION_BATCH_SIZE", 64)
            settings.OCR_PARALLEL_WORKERS = custom_get(
                self.config.custom_config, "OCR_PARALLEL_WORKERS", 0)
            settings.TEXIFY_BATCH_SIZE = custom_get(
                self.config.custom_config, "TEXIFY_BATCH_SIZE", 120)
            settings.LAYOUT_BATCH_SIZE = custom_get(
                self.config.custom_config, "LAYOUT_BATCH_SIZE", 120)
            settings.ORDER_BATCH_SIZE = custom_get(
                self.config.custom_config, "ORDER_BATCH_SIZE", 20)
            settings.TABLE_REC_BATCH_SIZE = custom_get(
                self.config.custom_config, "TABLE_REC_BATCH_SIZE", 120)

            settings.PAGINATE_OUTPUT = True
            settings.PAGE_SEPARATOR = "0110001001101100011001010110001101101111011001010111010101110010001000000110000101101110011001000010000001100001011100110110000101101100011011000110100101101110011001010110111000100000011101110110010101110010011001010010000001101000011001010111001001100101"  # Trust us
        return self.ocr_models[device_index]

    def process_fast_implementation(self, file_path: str) -> dict:
        pdf_doc = fitz.open(file_path)
        all_text = []
        embedded_images = []

        for page in pdf_doc:
            text = clean_text(page.get_text())
            if text.strip():
                all_text.append(text)

            for img_info in page.get_images(full=False):
                image = self._extract_image_from_pdf(pdf_doc, img_info[0])
                embedded_images.append(image)
                all_text.append("<attachment>")

        return create_sample(all_text, embedded_images)

    def process_implementation(self, file_path: str, temp_dir: str = "tmp/") -> dict:
        def extract_image_in_page(page, current_image_index) -> Tuple[List[str], int]:
            page_images = []
            num_image_in_page = page.count("<attachment>")
            for _ in range(num_image_in_page):
                page_images.append(IMAGE_LIST[current_image_index])
                current_image_index += 1

            return page_images, current_image_index

        def extract_file_list(index):
            files = set()
            file_list = []
            for _, filename in index:
                if filename not in files:
                    files.add(filename)
                    file_list.append(filename)
            return file_list

        model_lst = self.load_models()
        full_text, images, out_meta = convert_single_pdf(
            file_path, model_lst, batch_multiplier=BATCH_SIZE
        )

        # Save images so that the MD processor can find them
        new_images = {}
        os.makedirs(temp_dir, exist_ok=True)
        for image_path, pil_image in images.items():
            # Save the image in the temp directory
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png", dir=temp_dir)
            pil_image.save(temp_file.name)
            new_file_path = os.path.basename(temp_file.name)
            # Replace in the full text the path to the image with the new path
            new_images[new_file_path] = os.path.join(temp_dir, new_file_path)
            # Sometimes, the image path has a different spelling in the text, in the form of 0_Image_1.Png instead of 0_image_1.png
            other_way = image_path.replace(".png", ".Png").replace('image', 'Image')
            full_text = full_text.replace('[' + image_path, '[' + new_file_path).replace('(' + image_path,
                                                                                         '(' + new_file_path)  # Very sexy code alert
            full_text = full_text.replace('[' + other_way, '[' + new_file_path).replace('(' + other_way,
                                                                                        '(' + new_file_path)  # Very sexy code alert
        images = new_images

        content, _ = md_processor.MarkdownProcessor.process_md(
            full_text, temp_dir
        )

        split_pages = re.split(settings.PAGE_SEPARATOR, content)

        # get the index of the first pages of the files that were processed in this GPU
        # index is a dict with the file name as key and the index of the first page as value
        index = self.full_page_indexer[torch.cuda.current_device()]

        all_files = []
        all_images = []
        IMAGE_LIST = list(images.values())
        FILE_LIST = extract_file_list(index)

        current_doc_pages = []
        current_doc_images = []

        previous_file_name = None
        current_image_index = 0

        for page, (true_page_number, filename) in zip(split_pages, index):
            if previous_file_name is None:
                previous_file_name = filename

            if filename != previous_file_name:
                # We changed document
                previous_file_name = filename
                all_files.append("\n\n".join(current_doc_pages))
                all_images.append(current_doc_images)

                current_doc_pages.clear()
                current_doc_images.clear()

            current_doc_pages.append(page)
            images_in_page, current_image_index = extract_image_in_page(page, current_image_index)
            current_doc_images.extend(images_in_page)

        # Add last pages and images
        all_files.append("\n\n".join(current_doc_pages))
        all_images.append(current_doc_images)

        sample_list = create_sample_list_already_saved_images(all_files, all_images, FILE_LIST)
        return sample_list

    @staticmethod
    def _extract_image_from_pdf(pdf_doc, xref) -> Image.Image:
        base_image = pdf_doc.extract_image(xref)
        image_bytes = base_image["image"]
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")

    def split_files_across_gpus(self) -> List[List[FileDescriptor]]:
        """
        Split the files across GPUs.
        """
        chunked_files, self.full_page_indexer = merge_split_with_full_page_indexer(self.files, torch.cuda.device_count())

        return chunked_files

    @classmethod
    def get_file_len(cls, file: FileDescriptor) -> int:
        try:
            pdf_doc = fitz.open(file.file_path)
            return len(pdf_doc)
        except Exception as e:
            logger.error(
                f"Error while trying to get the number of pages of the PDF file {file.file_path}. Error: {str(e)}"
            )
            return -1
