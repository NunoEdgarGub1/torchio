"""
Custom implementation of

    Shaw et al., 2019
    MRI k-Space Motion Artefact Augmentation:
    Model Robustness and Task-Specific Uncertainty

"""

import warnings
from typing import Tuple, Optional, List
import torch
import numpy as np
from tqdm import tqdm
import SimpleITK as sitk
from scipy.linalg import logm, expm
from ....utils import is_image_dict
from ....torchio import INTENSITY, DATA, AFFINE, TYPE
from .. import Interpolation
from .. import RandomTransform


class RandomMotion(RandomTransform):
    """Add random MRI motion artifact.

    Args:
        degrees:
        translation:
        num_transforms:
        image_interpolation:
        proportion_to_augment:
        seed:

    """
    def __init__(
            self,
            degrees: float = 10,
            translation: float = 10,  # in mm
            num_transforms: int = 2,
            image_interpolation: Interpolation = Interpolation.LINEAR,
            proportion_to_augment: float = 1,
            seed: Optional[int] = None,
            ):
        super().__init__(seed=seed)
        self.degrees_range = self.parse_degrees(degrees)
        self.translation_range = self.parse_translation(translation)
        self.num_transforms = num_transforms
        self.image_interpolation = image_interpolation
        self.proportion_to_augment = self.parse_probability(
            proportion_to_augment,
            'proportion_to_augment',
        )

    def apply_transform(self, sample: dict) -> dict:
        for image_name, image_dict in sample.items():
            if not is_image_dict(image_dict):
                continue
            if image_dict[TYPE] != INTENSITY:
                continue
            params = self.get_params(
                self.degrees_range,
                self.translation_range,
                self.num_transforms,
                self.proportion_to_augment
            )
            times_params, degrees_params, translation_params, do_it = params
            keys = (
                'random_motion_times',
                'random_motion_degrees',
                'random_motion_translation',
                'random_motion_do',
            )
            for key, p in zip(keys, params):
                sample[image_name][key] = p
            if not do_it:
                return sample
            if (image_dict[DATA][0] < -0.1).any():
                # I use -0.1 instead of 0 because Python was warning me when
                # a value in a voxel was -7.191084e-35
                # There must be a better way of solving this
                message = (
                    f'Image "{image_name}" from "{image_dict["stem"]}"'
                    ' has negative values.'
                    ' Results can be unexpected because the transformed sample'
                    ' is computed as the absolute values'
                    ' of an inverse Fourier transform'
                )
                warnings.warn(message)
            image = self.nib_to_sitk(
                image_dict[DATA][0],
                image_dict[AFFINE],
            )
            transforms = self.get_rigid_transforms(
                degrees_params,
                translation_params,
                image,
            )
            image_dict[DATA] = self.add_artifact(
                image,
                transforms,
                times_params,
                self.image_interpolation,
            )
            # Add channels dimension
            image_dict[DATA] = image_dict[DATA][np.newaxis, ...]
            image_dict[DATA] = torch.from_numpy(image_dict[DATA])
        return sample

    @staticmethod
    def get_params(
            degrees_range: Tuple[float, float],
            translation_range: Tuple[float, float],
            num_transforms: int,
            probability: float,
            perturbation: float = 0.3,
            ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, bool]:
        """
        If perturbation is 0, the intervals between movements are constant
        """
        degrees_params = get_params_array(
            degrees_range, num_transforms)
        translation_params = get_params_array(
            translation_range, num_transforms)
        step = 1 / (num_transforms + 1)
        times = torch.arange(0, 1, step)[1:]
        noise = torch.FloatTensor(num_transforms)
        noise.uniform_(-step * perturbation, step * perturbation)
        times += noise
        times_params = times.numpy()
        do_it = torch.rand(1) < probability
        return times_params, degrees_params, translation_params, do_it

    def get_rigid_transforms(
            self,
            degrees_params: np.ndarray,
            translation_params: np.ndarray,
            image: sitk.Image,
            ) -> List[sitk.Euler3DTransform]:
        center_ijk = np.array(image.GetSize()) / 2
        center_lps = image.TransformContinuousIndexToPhysicalPoint(center_ijk)
        identity = np.eye(4)
        matrices = [identity]
        for degrees, translation in zip(degrees_params, translation_params):
            radians = np.radians(degrees).tolist()
            motion = sitk.Euler3DTransform()
            motion.SetCenter(center_lps)
            motion.SetRotation(*radians)
            motion.SetTranslation(translation.tolist())
            motion_matrix = self.transform_to_matrix(motion)
            matrices.append(motion_matrix)
        transforms = [self.matrix_to_transform(m) for m in matrices]
        return transforms

    @staticmethod
    def transform_to_matrix(transform: sitk.Euler3DTransform) -> np.ndarray:
        matrix = np.eye(4)
        rotation = np.array(transform.GetMatrix()).reshape(3, 3)
        matrix[:3, :3] = rotation
        matrix[:3, 3] = transform.GetTranslation()
        return matrix

    @staticmethod
    def matrix_to_transform(matrix: np.ndarray) -> sitk.Euler3DTransform:
        transform = sitk.Euler3DTransform()
        rotation = matrix[:3, :3].flatten().tolist()
        transform.SetMatrix(rotation)
        transform.SetTranslation(matrix[:3, 3])
        return transform

    def resample_images(
            self,
            image: sitk.Image,
            transforms: List[sitk.Euler3DTransform],
            interpolation: Interpolation,
            ) -> List[sitk.Image]:
        floating = reference = image
        default_value = np.float64(sitk.GetArrayViewFromImage(image).min())
        transforms = transforms[1:]  # first is identity
        images = [image]  # first is identity
        for transform in transforms:
            resampler = sitk.ResampleImageFilter()
            resampler.SetInterpolator(interpolation.value)
            resampler.SetReferenceImage(reference)
            resampler.SetOutputPixelType(sitk.sitkFloat32)
            resampler.SetDefaultPixelValue(default_value)
            resampler.SetTransform(transform)
            resampled = resampler.Execute(floating)
            images.append(resampled)
        return images

    @staticmethod
    def sort_spectra(spectra: np.ndarray, times: np.ndarray):
        """Use original spectrum to fill the center of k-space"""
        num_spectra = len(spectra)
        if np.any(times > 0.5):
            index = np.where(times > 0.5)[0].min()
        else:
            index = num_spectra - 1
        spectra[0], spectra[index] = spectra[index], spectra[0]

    def add_artifact(
            self,
            image: sitk.Image,
            transforms: List[sitk.Euler3DTransform],
            times: np.ndarray,
            interpolation: Interpolation,
            ):
        images = self.resample_images(image, transforms, interpolation)
        arrays = [sitk.GetArrayViewFromImage(im) for im in images]
        arrays = [array.transpose() for array in arrays]  # ITK to NumPy
        spectra = [self.fourier_transform(array) for array in arrays]
        self.sort_spectra(spectra, times)
        result_spectrum = np.empty_like(spectra[0])
        last_index = result_spectrum.shape[2]
        indices = (last_index * times).astype(int).tolist()
        indices.append(last_index)
        ini = 0
        for spectrum, fin in zip(spectra, indices):
            result_spectrum[..., ini:fin] = spectrum[..., ini:fin]
            ini = fin
        result_image = self.inv_fourier_transform(result_spectrum)
        return result_image.astype(np.float32)


def get_params_array(nums_range: Tuple[float, float], num_transforms: int):
    tensor = torch.FloatTensor(num_transforms, 3).uniform_(*nums_range)
    return tensor.numpy()
