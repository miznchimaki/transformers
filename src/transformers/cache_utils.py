import copy
import importlib.metadata
import json
import os
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Optional, Union

import torch
from packaging import version

from transformers.pytorch_utils import is_torch_greater_or_equal_than_2_6

from .configuration_utils import PretrainedConfig
from .utils import is_hqq_available, is_optimum_quanto_available, is_torch_greater_or_equal, logging


if is_hqq_available():
    from hqq.core.quantize import Quantizer as HQQQuantizer


logger = logging.get_logger(__name__)


# Utility functions for static/sliding cache update logic
def _static_cache_update(
    k_cache: torch.Tensor,
    v_cache: torch.Tensor,
    key_states: torch.Tensor,
    value_states: torch.Tensor,
    cache_position: Optional[torch.LongTensor],
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Updates the static cache tensors in place.

    Args:
        k_cache (`torch.Tensor`): The key cache tensor to update.
        v_cache (`torch.Tensor`): The value cache tensor to update.
        key_states (`torch.Tensor`): The new key states to add.
        value_states (`torch.Tensor`): The new value states to add.
        cache_position (`Optional[torch.LongTensor]`): The position indices where the new states should be inserted.
                                                       If None, the entire cache is overwritten (prefill).

    Returns:
        tuple[`torch.Tensor`, `torch.Tensor`]: The updated key and value cache tensors (modified in-place).
    """
    if cache_position is None:
        # Prefill phase where seq_len potentially equals max_cache_len. Directly copy.
        k_cache.copy_(key_states)
        v_cache.copy_(value_states)
    else:
        # Generation phase. Update specific positions.
        # Use index_copy_ for in-place update (compile-friendly).
        try:
            k_cache.index_copy_(2, cache_position, key_states)
            v_cache.index_copy_(2, cache_position, value_states)
        except NotImplementedError:
            # Fallback for devices like MPS where index_copy_ might not be supported.
            k_cache[:, :, cache_position] = key_states
            v_cache[:, :, cache_position] = value_states
    return k_cache, v_cache


def _sliding_cache_update(
    k_cache: torch.Tensor,
    v_cache: torch.Tensor,
    key_states: torch.Tensor,
    value_states: torch.Tensor,
    cache_position: torch.LongTensor,
    max_cache_len: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Updates the sliding window cache tensors, returning the potentially modified tensors.

    Args:
        k_cache (`torch.Tensor`): The key cache tensor to update.
        v_cache (`torch.Tensor`): The value cache tensor to update.
        key_states (`torch.Tensor`): The new key states to add.
        value_states (`torch.Tensor`): The new value states to add.
        cache_position (`torch.LongTensor`): The position indices where the new states should be inserted.
        max_cache_len (`int`): The maximum length of the sliding window cache.

    Returns:
        tuple[`torch.Tensor`, `torch.Tensor`]: The key and value tensors representing the cache state after the update.
                                               For prefill > window, these are the full input states.
                                               Otherwise, they are the updated cache tensors.
    """
    # Handle prefill phase when prompt length > sliding_window_size
    if cache_position.shape[0] > max_cache_len:
        new_k = key_states[:, :, -max_cache_len:, :]
        new_v = value_states[:, :, -max_cache_len:, :]
        k_cache.copy_(new_k)
        v_cache.copy_(new_v)
        return key_states, value_states

    # Sliding window logic for generation phase or prefill < window
    slicing = torch.arange(max_cache_len, device=value_states.device)
    current_seq_len = cache_position[-1] + 1  # Use last position to determine current length
    to_shift = current_seq_len > max_cache_len
    indices = (slicing + to_shift.sum()) % max_cache_len

    k_out_shifted = k_cache[:, :, indices]
    v_out_shifted = v_cache[:, :, indices]

    # Clamp cache_position to determine the *target index* within the shifted cache view
    update_position = cache_position.clamp(min=0, max=max_cache_len - 1)

    try:
        k_out_updated = k_out_shifted.index_copy(2, update_position, key_states)
        v_out_updated = v_out_shifted.index_copy(2, update_position, value_states)
    except NotImplementedError:
        # Fallback for MPS: clone and modify the clone
        k_out_updated = k_out_shifted.clone()
        v_out_updated = v_out_shifted.clone()
        k_out_updated[:, :, update_position] = key_states
        v_out_updated[:, :, update_position] = value_states

    k_cache.copy_(k_out_updated)
    v_cache.copy_(v_out_updated)
    return k_out_updated, v_out_updated


class Cache:
    """
    Base, abstract class for all caches. The actual data structure is specific to each subclass.
    """

    is_compileable = False

    def __init__(self):
        super().__init__()

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Updates the cache with the new `key_states` and `value_states` for the layer `layer_idx`.

        Parameters:
            key_states (`torch.Tensor`):
                The new key states to cache.
            value_states (`torch.Tensor`):
                The new value states to cache.
            layer_idx (`int`):
                The index of the layer to cache the states for.
            cache_kwargs (`dict[str, Any]`, `optional`):
                Additional arguments for the cache subclass. These are specific to each subclass and allow new types of
                cache to be created.

        Return:
            A tuple containing the updated key and value states.
        """
        raise NotImplementedError("Make sure to implement `update` in a subclass.")

    def get_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states. A layer index can be optionally passed."""
        # TODO: deprecate this function in favor of `cache_position`
        raise NotImplementedError("Make sure to implement `get_seq_length` in a subclass.")

    def get_max_cache_shape(self) -> Optional[int]:
        """Returns the maximum sequence length (i.e. max capacity) of the cache object"""
        raise NotImplementedError("Make sure to implement `get_max_cache_shape` in a subclass.")

    def get_usable_length(self, new_seq_length: int, layer_idx: Optional[int] = 0) -> int:
        """Given the sequence length of the new inputs, returns the usable length of the cache."""
        # Cache without size limit -> all cache is usable
        # Cache with size limit -> if the length cache plus the length of the new inputs is larger the maximum cache
        #   length, we will need to evict part of the cache (and thus not all cache is usable)
        max_length = self.get_max_cache_shape()
        previous_seq_length = self.get_seq_length(layer_idx)
        if max_length is not None and previous_seq_length + new_seq_length > max_length:
            return max_length - new_seq_length
        return previous_seq_length

    def reorder_cache(self, beam_idx: torch.LongTensor):
        """Reorders the cache for beam search, given the selected beam indices."""
        for layer_idx in range(len(self.key_cache)):
            if self.key_cache[layer_idx].numel():
                device = self.key_cache[layer_idx].device
                self.key_cache[layer_idx] = self.key_cache[layer_idx].index_select(0, beam_idx.to(device))
            if self.value_cache[layer_idx].numel():
                device = self.value_cache[layer_idx].device
                self.value_cache[layer_idx] = self.value_cache[layer_idx].index_select(0, beam_idx.to(device))

    def get_mask_sizes(self, cache_position: torch.Tensor, layer_idx: int) -> tuple[int, int]:
        """
        Return a tuple (kv_length, kv_offset) corresponding to the length and offset that will be returned for
        the given layer at `layer_idx`.
        The masks are then prepared according to the given lengths (kv_length, kv_offset) and patterns (i.e. sliding_window, chunk_size),
        for each layer.
        """
        query_length = cache_position.shape[0]
        past_seen_tokens = self.get_seq_length()
        kv_length = query_length + past_seen_tokens
        return kv_length, 0


@dataclass
class CacheConfig:
    """
    Base class for cache configs
    """

    cache_implementation: None

    @classmethod
    def from_dict(cls, config_dict, **kwargs):
        """
        Constructs a CacheConfig instance from a dictionary of parameters.
        Args:
            config_dict (dict[str, Any]): Dictionary containing configuration parameters.
            **kwargs: Additional keyword arguments to override dictionary values.

        Returns:
            CacheConfig: Instance of CacheConfig constructed from the dictionary.
        """
        config = cls(**config_dict)
        to_remove = []
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)
                to_remove.append(key)
        for key in to_remove:
            kwargs.pop(key, None)
        return config

    # Copied from transformers.utils.quantization_config.QuantizationConfigMixin.to_json_file
    def to_json_file(self, json_file_path: Union[str, os.PathLike]):
        """
        Save this instance to a JSON file.

        Args:
            json_file_path (`str` or `os.PathLike`):
                Path to the JSON file in which this configuration instance's parameters will be saved.
            use_diff (`bool`, *optional*, defaults to `True`):
                If set to `True`, only the difference between the config instance and the default
                `QuantizationConfig()` is serialized to JSON file.
        """
        with open(json_file_path, "w", encoding="utf-8") as writer:
            config_dict = self.to_dict()
            json_string = json.dumps(config_dict, indent=2, sort_keys=True) + "\n"

            writer.write(json_string)

    # Copied from transformers.utils.quantization_config.QuantizationConfigMixin.to_dict
    def to_dict(self) -> dict[str, Any]:
        """
        Serializes this instance to a Python dictionary. Returns:
            `dict[str, Any]`: Dictionary of all the attributes that make up this configuration instance.
        """
        return copy.deepcopy(self.__dict__)

    # Copied from transformers.utils.quantization_config.QuantizationConfigMixin.__iter__
    def __iter__(self):
        """allows `dict(obj)` for situations where obj may be a dict or QuantizationConfigMixin"""
        for attr, value in copy.deepcopy(self.__dict__).items():
            yield attr, value

    # Copied from transformers.utils.quantization_config.QuantizationConfigMixin.__repr__
    def __repr__(self):
        return f"{self.__class__.__name__} {self.to_json_string()}"

    def to_json_string(self):
        """
        Serializes this instance to a JSON formatted string.
        Returns:
            str: JSON formatted string representing the configuration instance.
        """
        return json.dumps(self.__dict__, indent=2) + "\n"

    # Copied from transformers.utils.quantization_config.QuantizationConfigMixin.update
    def update(self, **kwargs):
        """
        Updates attributes of this class instance with attributes from `kwargs` if they match existing attributes,
        returning all the unused kwargs.

        Args:
            kwargs (`dict[str, Any]`):
                Dictionary of attributes to tentatively update this class.

        Returns:
            `dict[str, Any]`: Dictionary containing all the key-value pairs that were not used to update the instance.
        """
        to_remove = []
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                to_remove.append(key)

        # Remove all the attributes that were updated, without modifying the input dict
        unused_kwargs = {key: value for key, value in kwargs.items() if key not in to_remove}
        return unused_kwargs


@dataclass
class QuantizedCacheConfig(CacheConfig):
    """
    Configuration class for quantized cache settings.

    Attributes:
        backend (`str`, *optional*, defaults to `"quanto"`):
            Backend to use when performing quantization, Can be one of [`quanto`, `HQQ`]
        nbits (`Optional[int]`, *optional*, defaults to 4):
            Number of bits, can be 2 or 4 for the `quanto` backend and one of [1, 2, 3, 4, 8] for the `HQQ` backend. Defaults to 2.
        axis_key (`int`, *optional*, defaults to 0):
            Axis over which to perform grouping for the key tensors. Can be [0, -1] for `quanto` backend and [0, 1] for `HQQ` backend.
        axis_value (`int`, *optional*, defaults to 0):
            Axis over which to perform grouping for the value tensors. Can be [0, -1] for `quanto` backend and [0, 1] for `HQQ` backend.
        q_group_size (`Optional[int]`, *optional*, defaults to 64):
            Size of the quantization group, should be a divisor of the model's hidden dimension.
            Defaults to 64.
        residual_length (`Optional[int]`, *optional*, defaults to 128):
            Length of the residual cache which will always be stored in original precision.
            Defaults to 128.
        compute_dtype (`torch.dtype`, *optional*, defaults to `torch.float16`):
            The default dtype used for computations in the model. Keys and Values will be cast to this dtype after dequantization.
        device (`str`, *optional*, defaults to `"cpu"`):
            Device on which to perform computations, should be same as the model's device.
    """

    def __init__(
        self,
        backend: str = "quanto",
        nbits: Optional[int] = 4,
        axis_key: Optional[int] = 0,
        axis_value: Optional[int] = 0,
        q_group_size: Optional[int] = 64,
        residual_length: Optional[int] = 128,
        compute_dtype: Optional[torch.dtype] = torch.float16,
        device: Optional[str] = "cpu",
    ):
        self.backend = backend
        self.nbits = nbits
        self.axis_key = axis_key
        self.axis_value = axis_value
        self.q_group_size = q_group_size
        self.residual_length = residual_length
        self.compute_dtype = compute_dtype
        self.device = device

    def validate(self):
        """Validates if the arguments passed are correct"""

        incorrect_arg_msg = (
            "Some of the keys in `cache_config` are defined incorrectly. `{key}` should be {correct_value}` "
            "but found {found_value}"
        )
        # Check that the values are reasonable in general (nbits, axis)
        # Later in QuantizedCache init we check if they are supported for that particular backend
        if self.nbits not in [1, 2, 3, 4, 8]:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="nbits",
                    correct_value="2 or 4 or 8",
                    found_value=self.nbits,
                ),
            )
        if self.q_group_size <= 0:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="q_group_size",
                    correct_value="a positive integer",
                    found_value=self.q_group_size,
                ),
            )
        if self.residual_length < 0:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="residual_length",
                    correct_value="a positive integer",
                    found_value=self.residual_length,
                ),
            )

        if self.axis_key not in [0, 1, -1]:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="axis_key",
                    correct_value="`1` or `0`, `-1`",
                    found_value=self.axis_key,
                ),
            )

        if self.axis_value not in [0, 1, -1]:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="axis_value",
                    correct_value="`1` or `0` or `-1`",
                    found_value=self.axis_value,
                ),
            )


@dataclass
class StaticCacheConfig(CacheConfig):
    """
    Configuration class for static cache settings.
    """

    cache_implementation = "static"

    def __init__(self, batch_size: int, max_cache_len: int, device="cpu"):
        self.batch_size = batch_size
        self.max_cache_len = max_cache_len
        self.device = device

    def validate(self):
        """Validates if the arguments passed are correct"""

        incorrect_arg_msg = (
            "Some of the keys in `cache_config` are defined incorrectly. `{key}` should be {correct_value}` "
            "but found {found_value}"
        )

        if self.batch_size <= 0:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="batch_size",
                    correct_value="> 0",
                    found_value=self.batch_size,
                ),
            )

        if self.max_cache_len <= 0:
            raise ValueError(
                incorrect_arg_msg.format(
                    key="max_cache_len",
                    correct_value="> 0",
                    found_value=self.max_cache_len,
                ),
            )


class DynamicCache(Cache):
    """
    A cache that grows dynamically as more tokens are generated. This is the default for generative models.

    It stores the Key and Value states as a list of tensors, one for each layer. The expected shape for each tensor is
    `[batch_size, num_heads, seq_len, head_dim]`.

    Example:

        ```python
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, DynamicCache

        >>> model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2-0.5B-Instruct")
        >>> tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2-0.5B-Instruct")

        >>> inputs = tokenizer(text="My name is Qwen2", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> past_key_values = DynamicCache()
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        DynamicCache()
        ```
    """

    def __init__(self, _distributed_cache_data: Optional[Iterable] = None) -> None:
        super().__init__()
        self.key_cache: list[torch.Tensor] = []
        self.value_cache: list[torch.Tensor] = []

        # `_distributed_cache_data` was originally added for compatibility with `torch.distributed` (DDP). See #36121
        # and #36373 for more information. In a nutshell, it is `map(gather_map, zip(*caches))`, i.e. each item in the
        # iterable contains the key and value states for a layer gathered across replicas by torch.distributed
        # (shape=[global batch size, num_heads, seq_len, head_dim]).
        # WARNING: `_distributed_cache_data` must be the first argument in `__init__`, otherwise we'll break
        # compatibility. The name of the argument doesn't matter.
        if _distributed_cache_data is not None:
            for key_states, value_states in _distributed_cache_data:
                self.key_cache.append(key_states)
                self.value_cache.append(value_states)

    def __getitem__(self, layer_idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Support for backwards-compatible `past_key_value` indexing, e.g. `past_key_value[0][0].shape[2]` to get the
        sequence length.
        """
        if layer_idx < len(self):
            return (self.key_cache[layer_idx], self.value_cache[layer_idx])
        else:
            raise KeyError(f"Cache only has {len(self)} layers, attempted to access layer with index {layer_idx}")

    def __iter__(self):
        """
        Support for backwards-compatible `past_key_value` iteration, e.g. `for x in past_key_value:` to iterate over
        keys and values
        """
        for layer_idx in range(len(self)):
            yield (self.key_cache[layer_idx], self.value_cache[layer_idx])

    def __len__(self):
        """
        Support for backwards-compatible `past_key_value` length, e.g. `len(past_key_value)`. This value corresponds
        to the number of layers in the model.
        """
        return len(self.key_cache)

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Updates the cache with the new `key_states` and `value_states` for the layer `layer_idx`.

        Parameters:
            key_states (`torch.Tensor`):
                The new key states to cache.
            value_states (`torch.Tensor`):
                The new value states to cache.
            layer_idx (`int`):
                The index of the layer to cache the states for.
            cache_kwargs (`dict[str, Any]`, `optional`):
                Additional arguments for the cache subclass. No additional arguments are used in `DynamicCache`.

        Return:
            A tuple containing the updated key and value states.
        """
        # Update the cache
        if key_states is not None:
            if len(self.key_cache) <= layer_idx:
                # There may be skipped layers, fill them with empty lists
                for _ in range(len(self.key_cache), layer_idx):
                    self.key_cache.append(torch.tensor([]))
                    self.value_cache.append(torch.tensor([]))
                self.key_cache.append(key_states)
                self.value_cache.append(value_states)
            elif (
                not self.key_cache[layer_idx].numel()  # prefers not t.numel() to len(t) == 0 to export the model
            ):  # fills previously skipped layers; checking for tensor causes errors
                self.key_cache[layer_idx] = key_states
                self.value_cache[layer_idx] = value_states
            else:
                self.key_cache[layer_idx] = torch.cat([self.key_cache[layer_idx], key_states], dim=-2)
                self.value_cache[layer_idx] = torch.cat([self.value_cache[layer_idx], value_states], dim=-2)

        return self.key_cache[layer_idx], self.value_cache[layer_idx]

    def get_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states. A layer index can be optionally passed."""
        # TODO: deprecate this function in favor of `cache_position`
        is_empty_layer = (
            len(self.key_cache) == 0  # no cache in any layer
            or len(self.key_cache) <= layer_idx  # skipped `layer_idx` and hasn't run a layer with cache after it
            or not self.key_cache[layer_idx].numel()  # the layer has no cache
        )
        layer_seq_length = self.key_cache[layer_idx].shape[-2] if not is_empty_layer else 0
        return layer_seq_length

    def get_max_cache_shape(self) -> Optional[int]:
        """Returns the maximum sequence length of the cache object. DynamicCache does not have a maximum length."""
        return None

    def to_legacy_cache(self) -> tuple[tuple[torch.Tensor, torch.Tensor]]:
        """Converts the `DynamicCache` instance into the its equivalent in the legacy cache format. Used for
        backward compatibility."""
        legacy_cache = ()
        for layer_idx in range(len(self)):
            legacy_cache += ((self.key_cache[layer_idx], self.value_cache[layer_idx]),)
        return legacy_cache

    @classmethod
    def from_legacy_cache(
        cls, past_key_values: Optional[tuple[tuple[torch.FloatTensor, torch.FloatTensor]]] = None
    ) -> "DynamicCache":
        """Converts a cache in the legacy cache format into an equivalent `DynamicCache`. Used for
        backward compatibility."""
        cache = cls()
        if past_key_values is not None:
            for layer_idx in range(len(past_key_values)):
                key_states, value_states = past_key_values[layer_idx]
                cache.update(key_states, value_states, layer_idx)
        return cache

    def crop(self, max_length: int):
        """Crop the past key values up to a new `max_length` in terms of tokens. `max_length` can also be
        negative to remove `max_length` tokens. This is used in assisted decoding and contrastive search."""
        # In case it is negative
        if max_length < 0:
            max_length = self.get_seq_length() - abs(max_length)

        if self.get_seq_length() <= max_length:
            return

        for idx in range(len(self.key_cache)):
            if self.key_cache[idx].numel():
                self.key_cache[idx] = self.key_cache[idx][..., :max_length, :]
                self.value_cache[idx] = self.value_cache[idx][..., :max_length, :]

    def batch_split(self, full_batch_size: int, split_size: int) -> list["DynamicCache"]:
        """Split the current instance into a list of `DynamicCache` by the batch size. This will be used by
        `_split_model_inputs()` in `generation.utils`"""
        out = []
        for i in range(0, full_batch_size, split_size):
            current_split = DynamicCache()
            current_split.key_cache = [tensor[i : i + split_size] for tensor in self.key_cache]
            current_split.value_cache = [tensor[i : i + split_size] for tensor in self.value_cache]
            out.append(current_split)
        return out

    @classmethod
    def from_batch_splits(cls, splits: list["DynamicCache"]) -> "DynamicCache":
        """This is the opposite of the above `batch_split()` method. This will be used by `stack_model_outputs` in
        `generation.utils`"""
        cache = cls()
        for idx in range(len(splits[0])):
            key_cache = [current.key_cache[idx] for current in splits if current.key_cache[idx].numel()]
            value_cache = [current.value_cache[idx] for current in splits if current.value_cache[idx].numel()]
            if key_cache != []:
                layer_keys = torch.cat(key_cache, dim=0)
                layer_values = torch.cat(value_cache, dim=0)
                cache.update(layer_keys, layer_values, idx)
        return cache

    def batch_repeat_interleave(self, repeats: int):
        """Repeat the cache `repeats` times in the batch dimension. Used in contrastive search."""
        for layer_idx in range(len(self)):
            self.key_cache[layer_idx] = self.key_cache[layer_idx].repeat_interleave(repeats, dim=0)
            self.value_cache[layer_idx] = self.value_cache[layer_idx].repeat_interleave(repeats, dim=0)

    def batch_select_indices(self, indices: torch.Tensor):
        """Only keep the `indices` in the batch dimension of the cache. Used in contrastive search."""
        for layer_idx in range(len(self)):
            self.key_cache[layer_idx] = self.key_cache[layer_idx][indices, ...]
            self.value_cache[layer_idx] = self.value_cache[layer_idx][indices, ...]


# Utilities for `DynamicCache` <> torch.export support
def _flatten_dynamic_cache(
    dynamic_cache: DynamicCache,
):
    """Flattens DynamicCache into flat list of tensors for `torch.export.export` to consume"""
    if not isinstance(dynamic_cache, DynamicCache):
        raise RuntimeError("This pytree flattening function should only be applied to DynamicCache")

    if not is_torch_greater_or_equal_than_2_6:
        logger.warning_once(
            "DynamicCache + torch.export is tested on torch 2.6.0+ and may not work on earlier versions."
        )

    # NOTE it seems _seen_tokens is deprecated, so probably doesn't need tracking
    dictionary = {
        "key_cache": getattr(dynamic_cache, "key_cache"),
        "value_cache": getattr(dynamic_cache, "value_cache"),
    }
    return torch.utils._pytree._dict_flatten(dictionary)


def _flatten_with_keys_dynamic_cache(dynamic_cache: DynamicCache):
    dictionary = {
        "key_cache": getattr(dynamic_cache, "key_cache"),
        "value_cache": getattr(dynamic_cache, "value_cache"),
    }
    return torch.utils._pytree._dict_flatten_with_keys(dictionary)


def _unflatten_dynamic_cache(
    values,
    context: torch.utils._pytree.Context,
):
    dictionary = torch.utils._pytree._dict_unflatten(values, context)
    cache = DynamicCache()
    for k, v in dictionary.items():
        setattr(cache, k, v)
    return cache


def _flatten_dynamic_cache_for_fx(cache, spec):
    dictionary = {
        "key_cache": getattr(cache, "key_cache"),
        "value_cache": getattr(cache, "value_cache"),
    }
    return torch.fx._pytree._dict_flatten_spec(dictionary, spec)


if is_torch_greater_or_equal("2.3"):
    torch.utils._pytree.register_pytree_node(
        DynamicCache,
        _flatten_dynamic_cache,
        _unflatten_dynamic_cache,
        serialized_type_name=f"{DynamicCache.__module__}.{DynamicCache.__name__}",
        flatten_with_keys_fn=_flatten_with_keys_dynamic_cache,
    )
    # TODO (tmanlaibaatar) This won't be needed in torch 2.7.
    torch.fx._pytree.register_pytree_flatten_spec(DynamicCache, _flatten_dynamic_cache_for_fx)


class OffloadedCache(DynamicCache):
    """
    A drop-in replacement for DynamicCache that conserves accelerator(GPU, XPU) memory at the expense of more CPU memory.
    Useful for generating from models with very long context.

    In addition to the default accelerator stream, where all forward() computations happen,
    this class uses another stream, the prefetch stream, which it creates itself.
    Since scheduling of operations on separate streams happens independently, this class uses
    the prefetch stream to asynchronously prefetch the KV cache of layer k+1 when layer k is executing.
    The movement of the layer k-1 cache to the CPU is handled by the default stream as a simple way to
    ensure the eviction is scheduled after all computations on that cache are finished.
    """

    def __init__(self) -> None:
        if not (
            torch.cuda.is_available()
            or (is_torch_greater_or_equal("2.7", accept_dev=True) and torch.xpu.is_available())
        ):
            raise RuntimeError(
                "OffloadedCache can only be used with a GPU"
                + (" or XPU" if is_torch_greater_or_equal("2.7", accept_dev=True) else "")
            )

        super().__init__()
        self.original_device = []
        self.prefetch_stream = None
        self.prefetch_stream = (
            torch.Stream() if is_torch_greater_or_equal("2.7", accept_dev=True) else torch.cuda.Stream()
        )
        self.beam_idx = None  # used to delay beam search operations

    def prefetch_layer(self, layer_idx: int):
        "Starts prefetching the next layer cache"
        if layer_idx < len(self):
            with (
                self.prefetch_stream
                if is_torch_greater_or_equal("2.7", accept_dev=True)
                else torch.cuda.stream(self.prefetch_stream)
            ):
                # Prefetch next layer tensors to GPU
                device = self.original_device[layer_idx]
                self.key_cache[layer_idx] = self.key_cache[layer_idx].to(device, non_blocking=True)
                self.value_cache[layer_idx] = self.value_cache[layer_idx].to(device, non_blocking=True)

    def evict_previous_layer(self, layer_idx: int):
        "Moves the previous layer cache to the CPU"
        if len(self) > 2:
            # We do it on the default stream so it occurs after all earlier computations on these tensors are done
            prev_layer_idx = (layer_idx - 1) % len(self)
            self.key_cache[prev_layer_idx] = self.key_cache[prev_layer_idx].to("cpu", non_blocking=True)
            self.value_cache[prev_layer_idx] = self.value_cache[prev_layer_idx].to("cpu", non_blocking=True)

    def __getitem__(self, layer_idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        "Gets the cache for this layer to the device. Prefetches the next and evicts the previous layer."
        if layer_idx < len(self):
            # Evict the previous layer if necessary
            if is_torch_greater_or_equal("2.7", accept_dev=True):
                torch.accelerator.current_stream().synchronize()
            else:
                torch.cuda.current_stream().synchronize()
            self.evict_previous_layer(layer_idx)
            # Load current layer cache to its original device if not already there
            original_device = self.original_device[layer_idx]
            self.prefetch_stream.synchronize()
            key_tensor = self.key_cache[layer_idx]
            value_tensor = self.value_cache[layer_idx]
            # Now deal with beam search ops which were delayed
            if self.beam_idx is not None:
                self.beam_idx = self.beam_idx.to(original_device)
                key_tensor = key_tensor.index_select(0, self.beam_idx)
                value_tensor = value_tensor.index_select(0, self.beam_idx)
            # Prefetch the next layer
            self.prefetch_layer((layer_idx + 1) % len(self))
            return (key_tensor, value_tensor)
        else:
            raise KeyError(f"Cache only has {len(self)} layers, attempted to access layer with index {layer_idx}")

    def reorder_cache(self, beam_idx: torch.LongTensor):
        """Saves the beam indices and reorders the cache when the tensor is back to its device."""
        # We delay this operation until the tensors are back to their original
        # device because performing torch.index_select on the CPU is very slow
        del self.beam_idx
        self.beam_idx = beam_idx.clone()

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Updates the cache with the new `key_states` and `value_states` for the layer `layer_idx`.
        Parameters:
            key_states (`torch.Tensor`):
                The new key states to cache.
            value_states (`torch.Tensor`):
                The new value states to cache.
            layer_idx (`int`):
                The index of the layer to cache the states for.
            cache_kwargs (`dict[str, Any]`, `optional`):
                Additional arguments for the cache subclass. No additional arguments are used in `OffloadedCache`.
        Return:
            A tuple containing the updated key and value states.
        """
        # Update the cache
        if len(self.key_cache) < layer_idx:
            raise ValueError("OffloadedCache does not support model usage where layers are skipped. Use DynamicCache.")
        elif len(self.key_cache) == layer_idx:
            self.key_cache.append(key_states)
            self.value_cache.append(value_states)
            self.original_device.append(key_states.device)
            self.evict_previous_layer(layer_idx)
        else:
            key_tensor, value_tensor = self[layer_idx]
            self.key_cache[layer_idx] = torch.cat([key_tensor, key_states], dim=-2)
            self.value_cache[layer_idx] = torch.cat([value_tensor, value_states], dim=-2)

        return self.key_cache[layer_idx], self.value_cache[layer_idx]

    # According to https://docs.python.org/3/library/exceptions.html#NotImplementedError
    # if a method is not supposed to be supported in a subclass we should set it to None
    from_legacy_cache = None

    to_legacy_cache = None


class QuantizedCache(DynamicCache):
    """
    A quantizer cache similar to what is described in the [KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache paper](https://huggingface.co/papers/2402.02750).
    It allows the model to generate longer sequence length without allocating too much memory for Key and Value cache by applying quantization.

    The cache has two types of storage, one for original precision and one for the quantized cache. A `residual length` is set as a maximum capacity for the
    original precision cache. When the length goes beyond maximum capacity, the original precision cache is discarded and moved into the quantized cache. The
    quantization is done per-channel with a set `q_group_size` for both Keys and Values, in contrast to what was described in the paper.

    It stores Keys and Values a list of quantized tensors (tuples in case we need to store metadata), one for each layer. Additionally, it stores the Key and
    Value in original precision states as a list of tensors, one for each layer. The size of each tensor
    is `[batch_size, num_heads, seq_len - residual_length, head_dim]`
    """

    def __init__(self, cache_config: QuantizedCacheConfig) -> None:
        super().__init__()

        # Used only for QuantCache where the seq-length can't be inferred easily from cache contents
        self._seen_tokens = 0
        self._quantized_key_cache: list[torch.Tensor] = []
        self._quantized_value_cache: list[torch.Tensor] = []

        self.nbits = cache_config.nbits
        self.residual_length = cache_config.residual_length
        self.q_group_size = cache_config.q_group_size
        self.axis_key = cache_config.axis_key
        self.axis_value = cache_config.axis_value
        self.compute_dtype = cache_config.compute_dtype
        self.device = cache_config.device

        super().__init__()

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        # Update the number of seen tokens
        if layer_idx == 0:
            self._seen_tokens += key_states.shape[-2]

        if len(self.key_cache) < layer_idx:
            raise ValueError("QuantizedCache does not support model usage where layers are skipped. Use DynamicCache.")
        elif len(self.key_cache) == layer_idx:
            self._quantized_key_cache.append(self._quantize(key_states.contiguous(), axis=self.axis_key))
            self._quantized_value_cache.append(self._quantize(value_states.contiguous(), axis=self.axis_value))
            self.key_cache.append(torch.zeros(0, dtype=key_states.dtype, device=key_states.device))
            self.value_cache.append(torch.zeros(0, dtype=key_states.dtype, device=key_states.device))
            keys_to_return, values_to_return = key_states, value_states
        else:
            dequant_key = self._dequantize(self._quantized_key_cache[layer_idx])
            dequant_value = self._dequantize(self._quantized_value_cache[layer_idx])
            keys_to_return = [dequant_key, self.key_cache[layer_idx], key_states]
            values_to_return = [dequant_value, self.value_cache[layer_idx], value_states]

            keys_to_return = torch.cat(keys_to_return, dim=-2)
            values_to_return = torch.cat(values_to_return, dim=-2)
            if (
                self.key_cache[layer_idx].dim() == 4
                and self.key_cache[layer_idx].shape[-2] + 1 >= self.residual_length
            ):
                self._quantized_key_cache[layer_idx] = self._quantize(keys_to_return.contiguous(), axis=self.axis_key)
                self._quantized_value_cache[layer_idx] = self._quantize(
                    values_to_return.contiguous(), axis=self.axis_value
                )
                self.key_cache[layer_idx] = torch.zeros(0, dtype=key_states.dtype, device=key_states.device)
                self.value_cache[layer_idx] = torch.zeros(0, dtype=key_states.dtype, device=key_states.device)
            else:
                self.key_cache[layer_idx] = torch.cat([self.key_cache[layer_idx], key_states], dim=-2)
                self.value_cache[layer_idx] = torch.cat([self.value_cache[layer_idx], value_states], dim=-2)

        return keys_to_return, values_to_return

    def get_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states. A layer index can be optionally passed."""
        if len(self.key_cache) <= layer_idx:
            return 0
        # since we cannot get the seq_length of each layer directly and rely on `_seen_tokens` which is
        # updated every "layer_idx" == 0, this is a hack to get the actual seq_length for the given layer_idx
        # this part of code otherwise fails when used to verify attn_weight shape in some models
        return self._seen_tokens if layer_idx == 0 else self._seen_tokens - 1

    def _quantize(self, tensor, axis):
        """Quantizes a key/value using a defined quantization method."""
        raise NotImplementedError("Make sure to implement `_quantize` in a subclass.")

    def _dequantize(self, q_tensor):
        """Dequantizes back the tensor that was quantized by `self._quantize()`"""
        raise NotImplementedError("Make sure to implement `_dequantize` in a subclass.")


class QuantoQuantizedCache(QuantizedCache):
    """
    Quantized Cache class that uses `quanto` as a backend to perform quantization. Current implementation supports `int2` and `int4` dtypes only.

    Parameters:
        cache_config (`QuantizedCacheConfig`):
            A configuration containing all the arguments to be used by the quantizer, including axis, qtype and group size.

    Example:

        ```python
        >>> # Run pip install quanto first if you don't have it yet
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, QuantoQuantizedCache, QuantizedCacheConfig

        >>> model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2-0.5B-Instruct")
        >>> tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2-0.5B-Instruct")

        >>> inputs = tokenizer(text="My name is Qwen2", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> cache_config = QuantizedCacheConfig(nbits=4)
        >>> past_key_values = QuantoQuantizedCache(cache_config=cache_config)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        QuantoQuantizedCache()
        ```
    """

    def __init__(self, cache_config: CacheConfig) -> None:
        super().__init__(cache_config)

        if is_optimum_quanto_available():
            optimum_quanto_version = version.parse(importlib.metadata.version("optimum-quanto"))
            if optimum_quanto_version <= version.parse("0.2.5"):
                raise ImportError(
                    f"You need optimum-quanto package version to be greater or equal than 0.2.5 to use `QuantoQuantizedCache`. Detected version {optimum_quanto_version}."
                )
            from optimum.quanto import MaxOptimizer, qint2, qint4

        if self.nbits not in [2, 4]:
            raise ValueError(f"`nbits` for `quanto` backend has to be one of [`2`, `4`] but got {self.nbits}")

        if self.axis_key not in [0, -1]:
            raise ValueError(f"`axis_key` for `quanto` backend has to be one of [`0`, `-1`] but got {self.axis_key}")

        if self.axis_value not in [0, -1]:
            raise ValueError(
                f"`axis_value` for `quanto` backend has to be one of [`0`, `-1`] but got {self.axis_value}"
            )

        self.qtype = qint4 if self.nbits == 4 else qint2
        self.optimizer = MaxOptimizer()  # hardcode as it's the only one for per-channel quantization

    def _quantize(self, tensor, axis):
        # We have two different API since in optimum-quanto, we don't use AffineQuantizer anymore
        if is_optimum_quanto_available():
            from optimum.quanto import quantize_weight

            scale, zeropoint = self.optimizer(tensor, self.qtype, axis, self.q_group_size)
            qtensor = quantize_weight(tensor, self.qtype, axis, scale, zeropoint, self.q_group_size)
            return qtensor

    def _dequantize(self, qtensor):
        return qtensor.dequantize()


class HQQQuantizedCache(QuantizedCache):
    """
    Quantized Cache class that uses `HQQ` as a backend to perform quantization. Current implementation supports `int2`, `int4`, `int8` dtypes.

    Parameters:
        cache_config (`QuantizedCacheConfig`):
            A configuration containing all the arguments to be used by the quantizer, including axis, qtype and group size.

    Example:

        ```python
        >>> # Run pip install hqq first if you don't have it yet
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, HQQQuantizedCache, QuantizedCacheConfig

        >>> model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2-0.5B-Instruct")
        >>> tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen2-0.5B-Instruct")

        >>> inputs = tokenizer(text="My name is Qwen2", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> cache_config = QuantizedCacheConfig(nbits=4, axis_key=1, axis_value=1)
        >>> past_key_values = HQQQuantizedCache(cache_config=cache_config)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        HQQQuantizedCache()
        ```
    """

    def __init__(self, cache_config: CacheConfig) -> None:
        super().__init__(cache_config)
        if self.nbits not in [1, 2, 3, 4, 8]:
            raise ValueError(
                f"`nbits` for `HQQ` backend has to be one of [`1`, `2`, `3`, `4`, `8`] but got {self.nbits}"
            )

        if self.axis_key not in [0, 1]:
            raise ValueError(f"`axis_key` for `HQQ` backend has to be one of [`0`, `1`] but got {self.axis_key}")

        if self.axis_value not in [0, 1]:
            raise ValueError(f"`axis_value` for `HQQ` backend has to be one of [`0`, `1`] but got {self.axis_value}")

        self.quantizer = HQQQuantizer

    def _quantize(self, tensor, axis):
        qtensor, meta = self.quantizer.quantize(
            tensor,
            axis=axis,
            device=self.device,
            compute_dtype=self.compute_dtype,
            nbits=self.nbits,
            group_size=self.q_group_size,
        )
        meta["compute_dtype"] = self.compute_dtype
        self.quantizer.cuda(qtensor, meta=meta, device=self.device)  # Move to device and cast to dtype
        meta["scale"] = meta["scale"].to(qtensor.device)
        meta["zero"] = meta["zero"].to(qtensor.device)
        return qtensor, meta

    def _dequantize(self, qtensor):
        quant_tensor, meta = qtensor
        tensor = self.quantizer.dequantize(quant_tensor, meta)
        return tensor


class SinkCache(Cache):
    """
    Is its now a `custom_generate` repository on the Hub: https://huggingface.co/transformers-community/sink_cache.
    See [these docs](https://huggingface.co/docs/transformers/generation_strategies#custom-decoding-methods) for
    general `custom_generate`usage.
    """

    # TODO (joao, manuel): Remove this class in v4.59.0
    def __init__(self, **kwargs) -> None:
        raise NotImplementedError(
            "`SinkCache` has been moved as a `custom_generate` repository on the Hub: "
            "https://huggingface.co/transformers-community/sink_cache. See the repository for usage examples."
        )


class StaticCache(Cache):
    """
    Static Cache class to be used with `torch.compile(model)` and `torch.export()`.

    Parameters:
        config (`PretrainedConfig`):
            The configuration file defining the shape-related attributes required to initialize the static cache.
        max_batch_size (`int`):
            The maximum batch size with which the model will be used. Note that a new instance must be instantiated if a
            smaller batch size is used. If you are manually setting the batch size, make sure to take into account the
            number of beams if you are running beam search
        max_cache_len (`int`, *optional*):
            The maximum sequence length with which the model will be used.
        device (`torch.device` or `str`, *optional*):
            The device on which the cache should be initialized. If you're using more than 1 computation device, you
            should pass the `layer_device_map` argument instead.
        dtype (`torch.dtype`, *optional*, defaults to `torch.float32`):
            The default `dtype` to use when initializing the layer.
        layer_device_map (`Optional[dict[int, Union[str, torch.device, int]]]]`, *optional*):
            Mapping between the layers and its device. This is required when you are manually initializing the cache
            and the model is split between different gpus. You can know which layers mapped to which device by
            checking the associated device_map: `model.hf_device_map`.
        tp_size (`Optional[int]`, *optional*):
            The tensor parallel size of the model. This is used to adjust the number of key/value heads in the cache
            if the model is using tensor parallelism. If not provided, it defaults to `None`, which means that the
            number of key/value heads will not be adjusted.


    Example:

        ```python
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, StaticCache

        >>> model = AutoModelForCausalLM.from_pretrained("meta-llama/Llama-2-7b-chat-hf")
        >>> tokenizer = AutoTokenizer.from_pretrained("meta-llama/Llama-2-7b-chat-hf")

        >>> inputs = tokenizer(text="My name is Llama", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> # Leave empty space for 10 new tokens, which can be used when calling forward iteratively 10 times to generate
        >>> max_generated_length = inputs.input_ids.shape[1] + 10
        >>> past_key_values = StaticCache(config=model.config, max_batch_size=1, max_cache_len=max_generated_length, device=model.device, dtype=model.dtype)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        StaticCache()
        ```
    """

    is_compileable = True

    def __init__(
        self,
        config: PretrainedConfig,
        max_batch_size: int,
        max_cache_len: Optional[int] = None,
        device: Union[torch.device, str, None] = None,
        dtype: torch.dtype = torch.float32,
        layer_device_map: Optional[dict[int, Union[str, torch.device, int]]] = None,
        tp_size: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.max_batch_size = max_batch_size
        self.max_cache_len = config.max_position_embeddings if max_cache_len is None else max_cache_len

        # Some model define a custom `head_dim` != config.hidden_size // config.num_attention_heads
        self.head_dim = getattr(config, "head_dim", None) or config.hidden_size // config.num_attention_heads

        self._dtype = dtype
        self.num_key_value_heads = (
            config.num_attention_heads
            if getattr(config, "num_key_value_heads", None) is None
            else config.num_key_value_heads
        )
        if tp_size is not None and tp_size > 1:
            if self.num_key_value_heads % tp_size != 0:
                raise ValueError(
                    f"Number of key value heads {self.num_key_value_heads} must be divisible by tensor parallel size {tp_size}."
                )
            # If the model is using tensor parallelism, we need to adjust the number of heads accordingly.
            self.num_key_value_heads //= tp_size

        self.key_cache: list[torch.Tensor] = []
        self.value_cache: list[torch.Tensor] = []
        # Note: There will be significant perf decrease if switching to use 5D tensors instead.
        cache_shape = (self.max_batch_size, self.num_key_value_heads, self.max_cache_len, self.head_dim)
        device = torch.device(device) if device is not None else None
        for idx in range(config.num_hidden_layers):
            if layer_device_map is not None:
                layer_device = layer_device_map[idx]
            else:
                layer_device = device
            new_layer_key_cache = torch.zeros(cache_shape, dtype=self._dtype, device=layer_device)
            new_layer_value_cache = torch.zeros(cache_shape, dtype=self._dtype, device=layer_device)
            # Note: `mark_static_address` is used to tag the cache as a fixed data pointer,
            # preventing compiled graph breaks when updating the cache.
            torch._dynamo.mark_static_address(new_layer_key_cache)
            torch._dynamo.mark_static_address(new_layer_value_cache)
            self.key_cache.append(new_layer_key_cache)
            self.value_cache.append(new_layer_value_cache)

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Updates the cache with the new `key_states` and `value_states` for the layer `layer_idx`.
        It is VERY important to index using a tensor, otherwise you introduce a copy to the device.

        Parameters:
            key_states (`torch.Tensor`):
                The new key states to cache.
            value_states (`torch.Tensor`):
                The new value states to cache.
            layer_idx (`int`):
                The index of the layer to cache the states for.
            cache_kwargs (`dict[str, Any]`, `optional`):
                Additional arguments for the cache subclass. The `StaticCache` needs the `cache_position` input
                to know how where to write in the cache.

        Return:
            A tuple containing the updated key and value states.
        """
        if cache_kwargs is None:
            cache_kwargs = {}

        key_states = key_states.to(self.key_cache[layer_idx].dtype)
        value_states = value_states.to(self.value_cache[layer_idx].dtype)
        return _static_cache_update(
            self.key_cache[layer_idx],
            self.value_cache[layer_idx],
            key_states,
            value_states,
            cache_kwargs.get("cache_position"),
        )

    def get_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states that were seen by the model."""
        # Occupied cache == any slot in the 3rd dim (sequence length) holds a non-zero value. To save on compute, let's
        # limit the check to the first batch member and head dimension.
        # TODO: deprecate this function in favor of `cache_position`
        return (self.key_cache[layer_idx][0, 0].any(dim=-1)).sum()

    def get_max_cache_shape(self) -> Optional[int]:
        return self.max_cache_len

    def reset(self):
        """Resets the cache values while preserving the objects"""
        for layer_idx in range(len(self.key_cache)):
            # In-place ops prevent breaking the static address
            self.key_cache[layer_idx].zero_()
            self.value_cache[layer_idx].zero_()

    def get_mask_sizes(self, cache_position: torch.Tensor, layer_idx: int) -> tuple[int, int]:
        """
        Return a tuple (kv_length, kv_offset) corresponding to the length and offset that will be returned for
        the given layer at `layer_idx`.
        The masks are then prepared according to the given lengths (kv_length, kv_offset) and patterns (i.e. sliding_window, chunk_size),
        for each layer.
        """
        kv_length = self.get_max_cache_shape()
        return kv_length, 0


class SlidingWindowCache(StaticCache):
    """
    Sliding Window Cache class to be used with `torch.compile` for models like Mistral that support sliding window attention.
    Every time when we try to update the cache, we compute the `indices` based on `cache_position >= self.config.sliding_window - 1`,
    if true(which means the cache can not hold all the old key value states and new states together because of the sliding window constraint),
    we need to do a cycle shift based on `indices` to replace the oldest states by the new key value states passed in.

    The `to_shift` is only true once we are above sliding_window. Thus with `sliding_window==64`:

    indices = (slicing + to_shift[-1].sum()-1) % self.config.sliding_window
    tensor([ 1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15, 16, 17, 18,
        19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36,
        37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54,
        55, 56, 57, 58, 59, 60, 61, 62, 63,  0])

    We overwrite the cache using these, then we always write at cache_position (clamped to `sliding_window`)

    Parameters:
        config (`PretrainedConfig`):
            The configuration file defining the shape-related attributes required to initialize the static cache.
        max_batch_size (`int`):
            The maximum batch size with which the model will be used. Note that a new instance must be instantiated if a
            smaller batch size is used.
        max_cache_len (`int`, *optional*):
            The maximum sequence length with which the model will be used.
        device (`torch.device` or `str`, *optional*):
            The device on which the cache should be initialized. If you're using more than 1 computation device, you
            should pass the `layer_device_map` argument instead.
        dtype (`torch.dtype`, *optional*, defaults to `torch.float32`):
            The default `dtype` to use when initializing the layer.
        layer_device_map (`Optional[dict[int, Union[str, torch.device, int]]]]`, *optional*):
            Mapping between the layers and its device. This is required when you are manually initializing the cache
            and the model is split between different gpus. You can know which layers mapped to which device by
            checking the associated device_map: `model.hf_device_map`.

    Example:

        ```python
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, SlidingWindowCache

        >>> model = AutoModelForCausalLM.from_pretrained("mistralai/Mistral-7B-Instruct-v0.3")
        >>> tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-Instruct-v0.3")

        >>> inputs = tokenizer(text="My name is Mistral", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> # Leave empty space for 10 new tokens, which can be used when calling forward iteratively 10 times to generate
        >>> max_generated_length = inputs.input_ids.shape[1] + 10
        >>> past_key_values = SlidingWindowCache(config=model.config, max_batch_size=1, max_cache_len=max_generated_length, device=model.device, dtype=model.dtype)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        SlidingWindowCache()
        ```
    """

    is_compileable = True

    def __init__(
        self,
        config: PretrainedConfig,
        max_batch_size: int,
        max_cache_len: Optional[int] = None,
        device: Union[torch.device, str, None] = None,
        dtype: torch.dtype = torch.float32,
        layer_device_map: Optional[dict[int, Union[str, torch.device, int]]] = None,
    ) -> None:
        if not hasattr(config, "sliding_window") or config.sliding_window is None:
            raise ValueError(
                "Setting `cache_implementation` to 'sliding_window' requires the model config supporting "
                "sliding window attention, please check if there is a `sliding_window` field in the model "
                "config and it's not set to None."
            )
        max_cache_len = min(config.sliding_window, max_cache_len)
        self.sliding_window = config.sliding_window
        super().__init__(
            config=config,
            max_batch_size=max_batch_size,
            max_cache_len=max_cache_len,
            device=device,
            dtype=dtype,
            layer_device_map=layer_device_map,
        )

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if cache_kwargs is None:
            cache_kwargs = {}
        cache_position = cache_kwargs.get("cache_position")

        if cache_position is None:
            raise ValueError("`cache_position` must be provided for SlidingWindowCache.")

        key_states = key_states.to(self.key_cache[layer_idx].dtype)
        value_states = value_states.to(self.value_cache[layer_idx].dtype)

        return _sliding_cache_update(
            self.key_cache[layer_idx],
            self.value_cache[layer_idx],
            key_states,
            value_states,
            cache_position,
            self.max_cache_len,
        )

    def get_max_cache_shape(self) -> Optional[int]:
        return self.max_cache_len

    def reset(self):
        for layer_idx in range(len(self.key_cache)):
            # In-place ops prevent breaking the static address
            self.key_cache[layer_idx].zero_()
            self.value_cache[layer_idx].zero_()

    def get_mask_sizes(self, cache_position: torch.Tensor, layer_idx: int) -> tuple[int, int]:
        """
        Return a tuple (kv_length, kv_offset) corresponding to the length and offset that will be returned for
        the given layer at `layer_idx`.
        The masks are then prepared according to the given lengths (kv_length, kv_offset) and patterns (i.e. sliding_window, chunk_size),
        for each layer.
        """
        query_length = cache_position.shape[0]
        first_cache_position = cache_position[0]
        # torch.clamp() is equivalent to max() but should be compile-friendly/exportable as first_cache_position is a Tensor
        kv_offset = torch.clamp(first_cache_position - self.sliding_window + 1, min=0)
        # This is not general (see HybridChunkedCache for the whole general case), but it's what the cache returns
        kv_length = max(query_length, self.get_max_cache_shape())
        return kv_length, kv_offset


class EncoderDecoderCache(Cache):
    """
    Base, abstract class for all encoder-decoder caches. Can be used to hold combinations of self-attention and
    cross-attention caches.

    Example:

        ```python
        >>> from transformers import AutoProcessor, AutoModelForCausalLM, DynamicCache, EncoderDecoderCache

        >>> model = AutoModelForCausalLM.from_pretrained("openai/whisper-small")
        >>> processor = AutoProcessor.from_pretrained("openai/whisper-small")

        >>> inputs = processor(audio=YOUR-AUDIO, return_tensors="pt")

        >>> # Prepare cache classes for encoder and decoder and pass it to model's forward
        >>> self_attention_cache = DynamicCache()
        >>> cross_attention_cache = DynamicCache()
        >>> past_key_values = EncoderDecoderCache(self_attention_cache, cross_attention_cache)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        EncoderDecoderCache()
        ```

    """

    def __init__(self, self_attention_cache: Cache, cross_attention_cache: Cache):
        super().__init__()
        self.self_attention_cache = self_attention_cache
        self.cross_attention_cache = cross_attention_cache
        self.is_compileable = getattr(self.self_attention_cache, "is_compileable", False)

        self.is_updated = {}
        for layer_idx in range(len(cross_attention_cache.key_cache)):
            self.is_updated[layer_idx] = bool(cross_attention_cache.get_seq_length(layer_idx) > 0)

    def __iter__(self):
        """
        Support for backwards-compatible `past_key_value` iteration, e.g. `for x in past_key_value:` to iterate over
        keys and values
        """
        for layer_idx in range(len(self)):
            yield (
                self.self_attention_cache.key_cache[layer_idx],
                self.self_attention_cache.value_cache[layer_idx],
                self.cross_attention_cache.key_cache[layer_idx],
                self.cross_attention_cache.value_cache[layer_idx],
            )

    def __getitem__(self, layer_idx: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Support for backwards-compatible `past_key_value` indexing, e.g. `past_key_value[0][0].shape[2]` to get the
        sequence length.
        """
        if layer_idx < len(self):
            return (
                self.self_attention_cache.key_cache[layer_idx],
                self.self_attention_cache.value_cache[layer_idx],
                self.cross_attention_cache.key_cache[layer_idx],
                self.cross_attention_cache.value_cache[layer_idx],
            )
        else:
            raise KeyError(f"Cache only has {len(self)} layers, attempted to access layer with index {layer_idx}")

    def __len__(self):
        """
        Support for backwards-compatible `past_key_value` length, e.g. `len(past_key_value)`. This value corresponds
        to the number of layers in the model.
        """
        return len(self.self_attention_cache)

    def to_legacy_cache(self) -> tuple[tuple[torch.Tensor]]:
        """Converts the `EncoderDecoderCache` instance into its equivalent in the legacy cache format."""
        legacy_cache = ()
        if len(self.cross_attention_cache) > 0:
            for self_attn, cross_attn in zip(
                self.self_attention_cache.to_legacy_cache(), self.cross_attention_cache.to_legacy_cache()
            ):
                legacy_cache += (self_attn + cross_attn,)
        else:
            legacy_cache = self.self_attention_cache.to_legacy_cache()
        return legacy_cache

    @classmethod
    def from_legacy_cache(
        cls, past_key_values: Optional[tuple[tuple[torch.FloatTensor]]] = None
    ) -> "EncoderDecoderCache":
        """Converts a cache in the legacy cache format into an equivalent `EncoderDecoderCache`."""
        cache = cls(
            self_attention_cache=DynamicCache(),
            cross_attention_cache=DynamicCache(),
        )
        if past_key_values is not None:
            for layer_idx in range(len(past_key_values)):
                key_states, value_states = past_key_values[layer_idx][:2]
                cache.self_attention_cache.update(key_states, value_states, layer_idx)
                if len(past_key_values[layer_idx]) > 2:
                    key_states, value_states = past_key_values[layer_idx][2:]
                    cache.cross_attention_cache.update(key_states, value_states, layer_idx)
                    cache.is_updated[layer_idx] = True
        return cache

    def get_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states. A layer index can be optionally passed."""
        # check if empty list because in case of static cache it will be a tensors and we can't check `if not torch.Tensor`
        return self.self_attention_cache.get_seq_length(layer_idx)

    def reset(self):
        if hasattr(self.self_attention_cache, "reset"):
            self.self_attention_cache.reset()
        if hasattr(self.cross_attention_cache, "reset"):
            self.cross_attention_cache.reset()
        elif not hasattr(self.self_attention_cache, "reset") and not hasattr(self.cross_attention_cache, "reset"):
            raise ValueError(
                "Neither self nor cross-attention cache have valid `.reset()` methods. `.reset()` should "
                "only be called on compatible cache classes, such as `StaticCache` or `SlidingWindowCache`. "
                f"Got {self.self_attention_cache.__str__()} for the self attention cache and "
                f"{self.cross_attention_cache.__str__()} for the cross attention cache."
            )
        for layer_idx in self.is_updated:
            self.is_updated[layer_idx] = False

    def reorder_cache(self, beam_idx: torch.LongTensor):
        """Reorders the cache for beam search, given the selected beam indices."""
        self.self_attention_cache.reorder_cache(beam_idx)
        self.cross_attention_cache.reorder_cache(beam_idx)

    def check_dynamic_cache(self, method: str):
        if not (
            isinstance(self.self_attention_cache, DynamicCache)
            and isinstance(self.cross_attention_cache, DynamicCache)
        ):
            raise ValueError(
                f"`{method}` is only defined for dynamic cache, got {self.self_attention_cache.__str__()} for the self "
                f"attention cache and {self.cross_attention_cache.__str__()} for the cross attention cache."
            )

    # TODO(gante, sanchit-gandhi): move following functionality into `.generate`
    def crop(self, maximum_length: int):
        """Crop the past key values up to a new `maximum_length` in terms of tokens. `maximum_length` can also be
        negative to remove `maximum_length` tokens. This is used in assisted decoding and contrastive search."""
        self.check_dynamic_cache(self.crop.__name__)
        self.self_attention_cache.crop(maximum_length)

    def batch_split(self, full_batch_size: int, split_size: int) -> "list[EncoderDecoderCache]":
        """Split the current instance into a list of `DynamicCache` by the batch size. This will be used by
        `_split_model_inputs()` in `generation.utils`"""
        self.check_dynamic_cache(self.batch_split.__name__)
        self_attention_cache = self.self_attention_cache.batch_split(full_batch_size, split_size)
        cross_attention_cache = self.cross_attention_cache.batch_split(full_batch_size, split_size)

        out = []
        for self_attn, cross_attn in zip(self_attention_cache, cross_attention_cache):
            out.append(EncoderDecoderCache(self_attn, cross_attn))
        return out

    @classmethod
    def from_batch_splits(cls, splits: list["EncoderDecoderCache"]) -> "EncoderDecoderCache":
        """This is the opposite of the above `batch_split()` method. This will be used by `stack_model_outputs` in
        `generation.utils`"""
        self_attention_cache = DynamicCache()
        cross_attention_cache = DynamicCache()
        for idx in range(len(splits[0])):
            layer_keys = torch.cat([current.self_attention_cache.key_cache[idx] for current in splits], dim=0)
            layer_values = torch.cat([current.self_attention_cache.value_cache[idx] for current in splits], dim=0)
            self_attention_cache.update(layer_keys, layer_values, idx)

            layer_keys = torch.cat([current.cross_attention_cache.key_cache[idx] for current in splits], dim=0)
            layer_values = torch.cat([current.cross_attention_cache.value_cache[idx] for current in splits], dim=0)
            cross_attention_cache.update(layer_keys, layer_values, idx)
        return cls(self_attention_cache, cross_attention_cache)

    def batch_repeat_interleave(self, repeats: int):
        """Repeat the cache `repeats` times in the batch dimension. Used in contrastive search."""
        self.check_dynamic_cache(self.batch_repeat_interleave.__name__)
        self.self_attention_cache.batch_repeat_interleave(repeats)
        self.cross_attention_cache.batch_repeat_interleave(repeats)

    def batch_select_indices(self, indices: torch.Tensor):
        """Only keep the `indices` in the batch dimension of the cache. Used in contrastive search."""
        self.check_dynamic_cache(self.batch_select_indices.__name__)
        self.self_attention_cache.batch_select_indices(indices)
        self.cross_attention_cache.batch_select_indices(indices)

    def get_max_cache_shape(self) -> Optional[int]:
        """Returns the maximum sequence length (i.e. max capacity) of the cache object"""
        return self.self_attention_cache.get_max_cache_shape()

    def get_mask_sizes(self, cache_position: torch.Tensor, layer_idx: int) -> tuple[int, int]:
        """
        Return a tuple (kv_length, kv_offset) corresponding to the length and offset that will be returned for
        the given layer at `layer_idx`.
        The masks are then prepared according to the given lengths (kv_length, kv_offset) and patterns (i.e. sliding_window, chunk_size),
        for each layer.
        """
        return self.self_attention_cache.get_mask_sizes(cache_position, layer_idx)


class HybridCache(Cache):
    """
    Hybrid Cache class to be used with `torch.compile` for models that alternate between a local sliding window
    attention and global attention in every other layer (originally implemented for Gemma2).
    Under the hood, Hybrid Cache leverages ["SlidingWindowCache"] for sliding window attention and ["StaticCache"]
    for global attention.For more information, see the documentation of each subcomponent cache class.

    Parameters:
        config (`PretrainedConfig):
            The configuration file defining the shape-related attributes required to initialize the static cache.
        max_batch_size (`int`):
            The maximum batch size with which the model will be used. Note that a new instance must be instantiated if a
            smaller batch size is used.
        max_cache_len (`int`, *optional*):
            The maximum sequence length with which the model will be used.
        device (`torch.device` or `str`, *optional*):
            The device on which the cache should be initialized. If you're using more than 1 computation device, you
            should pass the `layer_device_map` argument instead.
        dtype (torch.dtype, *optional*, defaults to `torch.float32`):
            The default `dtype` to use when initializing the layer.
        layer_device_map (`Optional[dict[int, Union[str, torch.device, int]]]]`, *optional*):
            Mapping between the layers and its device. This is required when you are manually initializing the cache
            and the model is split between different gpus. You can know which layers mapped to which device by
            checking the associated device_map: `model.hf_device_map`.
        tp_size (`Optional[int]`, *optional*):
            The tensor parallel size of the model. This is used to adjust the number of key/value heads in the cache
            if the model is using tensor parallelism. If not provided, it defaults to `None`, which means that the
            number of key/value heads will not be adjusted.

    Example:

        ```python
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, HybridCache

        >>> model = AutoModelForCausalLM.from_pretrained("google/gemma-2-2b")
        >>> tokenizer = AutoTokenizer.from_pretrained("google/gemma-2-2b")

        >>> inputs = tokenizer(text="My name is Gemma", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> # Leave empty space for 10 new tokens, which can be used when calling forward iteratively 10 times to generate
        >>> max_generated_length = inputs.input_ids.shape[1] + 10
        >>> past_key_values = HybridCache(config=model.config, max_batch_size=1, max_cache_len=max_generated_length, device=model.device, dtype=model.dtype)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        HybridCache()
        ```
    """

    is_compileable = True

    def __init__(
        self,
        config: PretrainedConfig,
        max_batch_size: int,
        max_cache_len: Optional[int] = None,
        device: Union[torch.device, str, None] = None,
        dtype: torch.dtype = torch.float32,
        layer_device_map: Optional[dict[int, Union[str, torch.device, int]]] = None,
        tp_size: Optional[int] = None,
    ) -> None:
        super().__init__()
        if not hasattr(config, "sliding_window") or config.sliding_window is None:
            raise ValueError(
                "Setting `cache_implementation` to 'hybrid' requires the model config supporting "
                "sliding window attention, please check if there is a `sliding_window` field in the model "
                "config and it's not set to None."
            )
        self.max_cache_len = max_cache_len if max_cache_len is not None else config.max_position_embeddings
        # Sliding layers can't be larger than the overall max cache len
        self.sliding_window_len = min(config.sliding_window, self.max_cache_len)
        self.max_batch_size = max_batch_size
        # Some model define a custom `head_dim` != config.hidden_size // config.num_attention_heads
        self.head_dim = (
            config.head_dim if hasattr(config, "head_dim") else config.hidden_size // config.num_attention_heads
        )

        self._dtype = dtype
        self.num_key_value_heads = (
            config.num_attention_heads
            if getattr(config, "num_key_value_heads", None) is None
            else config.num_key_value_heads
        )
        if tp_size is not None and tp_size > 1:
            if self.num_key_value_heads % tp_size != 0:
                raise ValueError(
                    f"Number of key value heads {self.num_key_value_heads} must be divisible by tensor parallel size {tp_size}."
                )
            # If the model is using tensor parallelism, we need to adjust the number of heads accordingly.
            self.num_key_value_heads //= tp_size

        # If the attribute does not exist in the config, fallback to a simple StaticCache
        if hasattr(config, "layer_types"):
            self.is_sliding = [layer_type != "full_attention" for layer_type in config.layer_types]
        else:
            self.is_sliding = [False] * config.num_hidden_layers

        self.key_cache: list[torch.Tensor] = []
        self.value_cache: list[torch.Tensor] = []
        global_cache_shape = (self.max_batch_size, self.num_key_value_heads, self.max_cache_len, self.head_dim)
        sliding_cache_shape = (self.max_batch_size, self.num_key_value_heads, self.sliding_window_len, self.head_dim)
        self.sliding_window = min(config.sliding_window, max_cache_len)
        device = torch.device(device) if device is not None else None
        for i in range(config.num_hidden_layers):
            if layer_device_map is not None:
                layer_device = layer_device_map[i]
            else:
                layer_device = device
            # Note: `mark_static_address` is used to tag the cache as an fixed data pointer, preventing cuda graph
            # breaks when updating the cache.
            cache_shape = sliding_cache_shape if self.is_sliding[i] else global_cache_shape
            new_layer_key_cache = torch.zeros(cache_shape, dtype=self._dtype, device=layer_device)
            new_layer_value_cache = torch.zeros(cache_shape, dtype=self._dtype, device=layer_device)
            torch._dynamo.mark_static_address(new_layer_key_cache)
            torch._dynamo.mark_static_address(new_layer_value_cache)
            self.key_cache.append(new_layer_key_cache)
            self.value_cache.append(new_layer_value_cache)

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if cache_kwargs is None:
            cache_kwargs = {}
        cache_position = cache_kwargs.get("cache_position")
        if cache_position is None:
            raise ValueError("`cache_position` must be provided for HybridCache.")

        is_sliding_layer = self.is_sliding[layer_idx]

        # These two `if` blocks are only reached in multigpu and if `layer_device_map` is not passed. They are used
        # when the cache is initialized in the forward pass (e.g. Gemma2)
        if self.key_cache[layer_idx].device != key_states.device:
            self.key_cache[layer_idx] = self.key_cache[layer_idx].to(key_states.device)
        if self.value_cache[layer_idx].device != value_states.device:
            self.value_cache[layer_idx] = self.value_cache[layer_idx].to(value_states.device)

        k_cache = self.key_cache[layer_idx]
        v_cache = self.value_cache[layer_idx]
        key_states = key_states.to(k_cache.dtype)
        value_states = value_states.to(v_cache.dtype)

        if is_sliding_layer:
            return _sliding_cache_update(
                k_cache,
                v_cache,
                key_states,
                value_states,
                cache_position,
                k_cache.shape[2],  # Use actual cache dim as max cache len
            )
        else:
            return _static_cache_update(k_cache, v_cache, key_states, value_states, cache_position)

    def get_max_cache_shape(self) -> Optional[int]:
        return self.max_cache_len

    def get_seq_length(self, layer_idx: Optional[int] = 0):
        # Occupied cache == any slot in the 3rd dim (sequence length) holds a non-zero value. To save on compute, let's
        # limit the check to the first batch member and head dimension.
        # TODO: deprecate this function in favor of `cache_position`
        if layer_idx != 0:
            raise ValueError(
                "`get_seq_length` on `HybridCache` may get inconsistent results depending on the layer index. "
                "Using the `layer_idx` argument is not supported."
            )
        return (self.key_cache[layer_idx][0, 0].any(dim=-1)).sum()

    def reset(self):
        """Resets the cache values while preserving the objects"""
        for layer_idx in range(len(self.key_cache)):
            # In-place ops prevent breaking the static address
            self.key_cache[layer_idx].zero_()
            self.value_cache[layer_idx].zero_()

    def get_mask_sizes(self, cache_position: torch.Tensor, layer_idx: int) -> tuple[int, int]:
        """
        Return a tuple (kv_length, kv_offset) corresponding to the length and offset that will be returned for
        the given layer at `layer_idx`.
        The masks are then prepared according to the given lengths (kv_length, kv_offset) and patterns (i.e. sliding_window, chunk_size),
        for each layer.
        """
        if self.is_sliding[layer_idx]:
            query_length = cache_position.shape[0]
            first_cache_position = cache_position[0]

            local_mask_kv_offset = torch.clamp(first_cache_position - self.sliding_window + 1, min=0)
            # This is not general (see HybridChunkedCache for the whole general case), but it's what the cache returns
            local_mask_kv_length = max(query_length, self.sliding_window)
            return local_mask_kv_length, local_mask_kv_offset

        full_mask_kv_offset = 0
        full_mask_kv_length = self.get_max_cache_shape()
        return full_mask_kv_length, full_mask_kv_offset


class HybridChunkedCache(Cache):
    """
    Hybrid Cache class to be used with `torch.compile` for models that alternate between a local sliding window
    attention and global attention in every other layer, with support for chunked attention (originally implemented
    for Llama4).
    Under the hood, Hybrid Cache leverages ["SlidingWindowCache"] for sliding window attention and ["StaticCache"]
    for global attention. For more information, see the documentation of each subcomponent cache class.

    Parameters:
        config (`PretrainedConfig):
            The configuration file defining the shape-related attributes required to initialize the static cache.
        max_batch_size (`int`):
            The maximum batch size with which the model will be used. Note that a new instance must be instantiated if a
            smaller batch size is used.
        max_cache_len (`int`, *optional*):
            The maximum sequence length with which the model will be used.
        device (`torch.device` or `str`, *optional*):
            The device on which the cache should be initialized. If you're using more than 1 computation device, you
            should pass the `layer_device_map` argument instead.
        dtype (torch.dtype, *optional*, defaults to `torch.bfloat16`):
            The default `dtype` to use when initializing the layer.
        layer_device_map (`Optional[dict[int, Union[str, torch.device, int]]]]`, *optional*):
            Mapping between the layers and its device. This is required when you are manually initializing the cache
            and the model is split between different gpus. You can know which layers mapped to which device by
            checking the associated device_map: `model.hf_device_map`.

    Example:

        ```python
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, HybridCache

        >>> model = AutoModelForCausalLM.from_pretrained("google/gemma-2-2b")
        >>> tokenizer = AutoTokenizer.from_pretrained("google/gemma-2-2b")

        >>> inputs = tokenizer(text="My name is Gemma", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> # Leave empty space for 10 new tokens, which can be used when calling forward iteratively 10 times to generate
        >>> max_generated_length = inputs.input_ids.shape[1] + 10
        >>> past_key_values = HybridCache(config=model.config, max_batch_size=1, max_cache_len=max_generated_length, device=model.device, dtype=model.dtype)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> outputs.past_key_values # access cache filled with key/values from generation
        HybridCache()
        ```
    """

    is_compileable = True

    def __init__(
        self,
        config: PretrainedConfig,
        max_batch_size: int,
        max_cache_len: Optional[int] = None,
        device: Union[torch.device, str, None] = None,
        dtype: torch.dtype = torch.bfloat16,
        layer_device_map: Optional[dict[int, Union[str, torch.device, int]]] = None,
    ) -> None:
        super().__init__()
        if not hasattr(config, "sliding_window") or config.sliding_window is None:
            self.sliding_window = getattr(config.get_text_config(), "attention_chunk_size", 8192)
        else:
            self.sliding_window = config.sliding_window
        self.max_cache_len = max_cache_len
        # Sliding layers can't be larger than the overall max cache len
        self.sliding_window = min(self.sliding_window, self.max_cache_len)
        self.max_batch_size = max_batch_size
        self.head_dim = getattr(config, "head_dim", config.hidden_size // config.num_attention_heads)
        self._dtype = dtype

        # If the attribute does not exist in the config, fallback to a simple StaticCache
        if hasattr(config, "layer_types"):
            self.is_sliding = [layer_type != "full_attention" for layer_type in config.layer_types]
        else:
            self.is_sliding = [False] * config.num_hidden_layers

        self.key_cache: list[torch.Tensor] = []
        self.value_cache: list[torch.Tensor] = []
        self.cumulative_length = [0 for _ in range(config.num_hidden_layers)]

    def initialise_cache_layer(self, layer_idx, key_states):
        if len(self.key_cache) > layer_idx:
            return

        num_key_value_heads = key_states.shape[1]
        device = key_states.device
        global_cache_shape = (self.max_batch_size, num_key_value_heads, self.max_cache_len, self.head_dim)
        sliding_cache_shape = (self.max_batch_size, num_key_value_heads, self.sliding_window, self.head_dim)
        # Note: `mark_static_address` is used to tag the cache as an fixed data pointer, preventing cuda graph
        # breaks when updating the cache.
        cache_shape = sliding_cache_shape if self.is_sliding[layer_idx] else global_cache_shape
        new_layer_key_cache = torch.zeros(cache_shape, dtype=self._dtype, device=device)
        new_layer_value_cache = torch.zeros(cache_shape, dtype=self._dtype, device=device)
        torch._dynamo.mark_static_address(new_layer_key_cache)
        torch._dynamo.mark_static_address(new_layer_value_cache)
        self.key_cache.append(new_layer_key_cache)
        self.value_cache.append(new_layer_value_cache)

    def _sliding_update(self, cache_position, layer_idx, key_states, value_states, k_out, v_out, max_cache_len):
        cumulative_length = self.cumulative_length[layer_idx]
        # Update it now that we saved the value above
        self.cumulative_length[layer_idx] += key_states.shape[-2]
        is_full = cumulative_length >= max_cache_len
        if is_full:
            full_key_states = torch.cat((k_out[:, :, 1:, :], key_states), dim=-2)
            full_value_states = torch.cat((v_out[:, :, 1:, :], value_states), dim=-2)
            # Fast decoding path -> here as the effective size is still sliding window, it is extremely important
            # to return `self.key_cache[layer_idx]` and `self.value_cache[layer_idx]`, as they have the fixed address
            # in memory (the values are the same as the full states, but not the address!!)
            if key_states.shape[-2] == 1:
                self.key_cache[layer_idx].copy_(full_key_states)
                self.value_cache[layer_idx].copy_(full_value_states)
                return self.key_cache[layer_idx], self.value_cache[layer_idx]
        elif not is_full and cumulative_length + key_states.shape[2] > max_cache_len:
            # Fast prefill path, no need to cat() in this case (which creates a copy even if cating from 0 dim)
            if cumulative_length == 0:
                full_key_states = key_states
                full_value_states = value_states
            else:
                full_key_states = torch.cat((k_out[:, :, :cumulative_length, :], key_states), dim=-2)
                full_value_states = torch.cat((v_out[:, :, :cumulative_length, :], value_states), dim=-2)
        else:
            self.key_cache[layer_idx].index_copy_(2, cache_position, key_states)
            self.value_cache[layer_idx].index_copy_(2, cache_position, value_states)
            return self.key_cache[layer_idx], self.value_cache[layer_idx]

        self.key_cache[layer_idx].copy_(full_key_states[:, :, -max_cache_len:, :])
        self.value_cache[layer_idx].copy_(full_value_states[:, :, -max_cache_len:, :])
        # we should return the whole states instead of k_out, v_out to take the whole prompt
        # into consideration when building kv cache instead of just throwing away tokens outside of the window
        return full_key_states, full_value_states

    def _static_update(self, cache_position, layer_idx, key_states, value_states, k_out, v_out, max_cache_len):
        k_out[:, :, cache_position] = key_states
        v_out[:, :, cache_position] = value_states

        self.key_cache[layer_idx] = k_out
        self.value_cache[layer_idx] = v_out
        return k_out, v_out

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        if cache_kwargs is None:
            cache_kwargs = {}
        cache_position = cache_kwargs.get("cache_position")
        self.initialise_cache_layer(layer_idx, key_states)

        k_out = self.key_cache[layer_idx]
        v_out = self.value_cache[layer_idx]
        key_states = key_states.to(k_out.dtype)
        value_states = value_states.to(v_out.dtype)

        update_fn = self._sliding_update if self.is_sliding[layer_idx] else self._static_update
        return update_fn(
            cache_position,
            layer_idx,
            key_states,
            value_states,
            k_out,
            v_out,
            k_out.shape[2],
        )

    def get_max_cache_shape(self) -> Optional[int]:
        return self.max_cache_len

    def get_seq_length(self, layer_idx: Optional[int] = 0):
        # Occupied cache == any slot in the 3rd dim (sequence length) holds a non-zero value. To save on compute, let's
        # limit the check to the first batch member and head dimension.
        # TODO: deprecate this function in favor of `cache_position`
        if layer_idx != 0:
            raise ValueError(
                "`get_seq_length` on `HybridCache` may get inconsistent results depending on the layer index. "
                "Using the `layer_idx` argument is not supported."
            )
        if len(self.key_cache) == 0:
            return 0
        return (self.key_cache[layer_idx][0, 0].any(dim=-1)).sum()

    def reset(self):
        """Resets the cache values while preserving the objects"""
        for layer_idx in range(len(self.key_cache)):
            # In-place ops prevent breaking the static address
            self.key_cache[layer_idx].zero_()
            self.value_cache[layer_idx].zero_()
        self.cumulative_length = [0 for _ in range(len(self.cumulative_length))]

    def get_mask_sizes(self, cache_position: torch.Tensor, layer_idx: int) -> tuple[int, int]:
        """
        Return a tuple (kv_length, kv_offset) corresponding to the length and offset that will be returned for
        the given layer at `layer_idx`.
        The masks are then prepared according to the given lengths (kv_length, kv_offset) and patterns (i.e. sliding_window, chunk_size),
        for each layer.
        """
        if self.is_sliding[layer_idx]:
            query_length = cache_position.shape[0]
            first_cache_position = cache_position[0]

            local_mask_kv_offset = torch.clamp(first_cache_position - self.sliding_window + 1, min=0)
            # This is the true general case for any Cache using local attention (sliding or chunked)
            if first_cache_position >= self.sliding_window:
                # Here the Cache is already full
                local_mask_kv_length = self.sliding_window + query_length - 1
            elif (
                first_cache_position < self.sliding_window
                and first_cache_position + query_length > self.sliding_window
            ):
                # Here the Cache becomes full with the new input
                local_mask_kv_length = first_cache_position + query_length
            else:
                # Here the Cache is still smaller than the local size, but we return the local size as it's static
                local_mask_kv_length = self.sliding_window
            return local_mask_kv_length, local_mask_kv_offset

        full_mask_kv_offset = 0
        full_mask_kv_length = self.get_max_cache_shape()
        return full_mask_kv_length, full_mask_kv_offset


class OffloadedHybridCache(HybridChunkedCache):
    def __init__(
        self,
        config: PretrainedConfig,
        max_batch_size: int,
        max_cache_len: Optional[int] = None,
        device: Union[torch.device, str, None] = None,
        dtype: torch.dtype = torch.bfloat16,
        offload_device: Union[str, torch.device] = torch.device("cpu"),
        layer_device_map: Optional[dict[int, Union[str, torch.device, int]]] = None,
    ):
        super().__init__(config, max_batch_size, max_cache_len, device, dtype, layer_device_map)

        # TODO (joao): to enable this cache on multiple devicesuse the pattern from `OffloadedCache`, which keeps
        # track of the original device of each layer
        unique_devices = set(layer_device_map.values()) if layer_device_map else set()
        if len(unique_devices) > 1:
            raise ValueError(f"OffloadedHybridCache does not support multiple devices. Got devices: {unique_devices}")

        self.offload_device = torch.device(offload_device)
        # Create new CUDA stream for parallel prefetching.
        self._prefetch_stream = torch.cuda.Stream() if torch._C._get_accelerator().type == "cuda" else None
        # Those will be dynamically created as the other layers (for TP)
        self.device_key_cache = None
        self.device_value_cache = None
        # This gives the index of which on-device full layer to use (we need 2 to avoid race conditions when prefetching)
        self.active_device_layer = 0

    def initialise_cache_layer(self, layer_idx, key_states):
        """Overridden to use the correct device if offloaded layer (and pin memory)."""
        if len(self.key_cache) > layer_idx:
            return

        num_key_value_heads = key_states.shape[1]
        device = key_states.device if self.is_sliding[layer_idx] else self.offload_device
        pin_memory = not self.is_sliding[layer_idx]
        global_cache_shape = (self.max_batch_size, num_key_value_heads, self.max_cache_len, self.head_dim)
        sliding_cache_shape = (self.max_batch_size, num_key_value_heads, self.sliding_window, self.head_dim)
        # Note: `mark_static_address` is used to tag the cache as an fixed data pointer, preventing cuda graph
        # breaks when updating the cache.
        cache_shape = sliding_cache_shape if self.is_sliding[layer_idx] else global_cache_shape
        new_layer_key_cache = torch.zeros(cache_shape, dtype=self._dtype, device=device, pin_memory=pin_memory)
        new_layer_value_cache = torch.zeros(cache_shape, dtype=self._dtype, device=device, pin_memory=pin_memory)
        torch._dynamo.mark_static_address(new_layer_key_cache)
        torch._dynamo.mark_static_address(new_layer_value_cache)
        self.key_cache.append(new_layer_key_cache)
        self.value_cache.append(new_layer_value_cache)

        # Make sure to initialize the on-device layer if it does not already exist
        if self.device_key_cache is None and not self.is_sliding[layer_idx]:
            self.device_key_cache = []
            self.device_value_cache = []
            # We need 2 layers to avoid race conditions when prefetching the next one
            for _ in range(2):
                device_layer_key_cache = torch.zeros(cache_shape, dtype=self._dtype, device=key_states.device)
                device_layer_value_cache = torch.zeros(cache_shape, dtype=self._dtype, device=key_states.device)
                torch._dynamo.mark_static_address(new_layer_key_cache)
                torch._dynamo.mark_static_address(new_layer_value_cache)
                self.device_key_cache.append(device_layer_key_cache)
                self.device_value_cache.append(device_layer_value_cache)

    def _static_update(self, cache_position, layer_idx, key_states, value_states, k_out, v_out, max_cache_len):
        # Wait for prefetch stream if needed
        if self._prefetch_stream is not None:
            torch.cuda.default_stream(key_states.device).wait_stream(self._prefetch_stream)

        # Get correct on-device layer
        k_out = self.device_key_cache[self.active_device_layer]
        v_out = self.device_value_cache[self.active_device_layer]

        # Let's prefetch the next layer as soon as possible
        self._prefetch_next_layer(layer_idx)

        # Copy to on-device layer
        k_out[:, :, cache_position] = key_states
        v_out[:, :, cache_position] = value_states

        # Copy to offloaded device
        self.key_cache[layer_idx][:, :, cache_position] = key_states.to(self.offload_device)
        self.value_cache[layer_idx][:, :, cache_position] = value_states.to(self.offload_device)

        return k_out, v_out

    def _prefetch_next_layer(self, layer_idx: int) -> None:
        """Based on current layer_idx, prefetch next full layer to the device."""

        # Switch the active layer
        self.active_device_layer = 0 if self.active_device_layer == 1 else 1

        # Find the next non-sliding layer
        try:
            next_layer = layer_idx + 1 + self.is_sliding[layer_idx + 1 :].index(False)
        # In this case, we are at the last layer, and we go back to prefect the first one
        except ValueError:
            next_layer = self.is_sliding.index(False)

        # Alternate between two on-device caches.
        if self._prefetch_stream is not None:
            with torch.cuda.stream(self._prefetch_stream):
                self._prefetch_layer_in_context(next_layer)
        else:
            self._prefetch_layer_in_context(next_layer)

    def _prefetch_layer_in_context(self, layer_idx: int) -> None:
        """Performs the actual copy of the layer to device cache."""
        if len(self.key_cache) > layer_idx:
            self.device_key_cache[self.active_device_layer].copy_(self.key_cache[layer_idx], non_blocking=True)
            self.device_value_cache[self.active_device_layer].copy_(self.value_cache[layer_idx], non_blocking=True)
        # The layer was not yet initialized
        else:
            self.device_key_cache[self.active_device_layer].fill_(0.0)
            self.device_value_cache[self.active_device_layer].fill_(0.0)


class OffloadedStaticCache(StaticCache):
    """
    Static cache class to be used with `torch.compile(model)` that offloads to the CPU or
    another device.

    Args:
        config (`PretrainedConfig):
            The configuration file defining the shape-related attributes required to initialize
            the static cache.
        max_batch_size (`int`):
            The maximum batch size with which the model will be used.
        max_cache_len (`int`):
            The maximum sequence length with which the model will be used.
        device (`Union[str, torch.device]`):
            The device on which the cache should be initialized. If you're using more than 1 computation device, you
            should pass the `layer_device_map` argument instead.
        dtype (`torch.dtype`, *optional*):
            The default `dtype` to use when initializing the cache.
        offload_device (`Union[str, torch.device]`, *optional*, defaults to `cpu`):
            The device to offload to. Defaults to CPU.
        layer_device_map (`dict[int, Union[str, torch.device, int]]`, *optional*):
            Mapping between the layers and its device. This is required when you are manually initializing the cache
            and the model is split between different gpus. You can know which layers mapped to which device by
            checking the associated device_map: `model.hf_device_map`.
        tp_size (`Optional[int]`, *optional*):
            The tensor parallel size of the model. This is used to adjust the number of key/value heads in the cache
            if the model is using tensor parallelism. If not provided, it defaults to `None`, which means that the
            number of key/value heads will not be adjusted.

    Example:

        ```python
        >>> from transformers import AutoTokenizer, AutoModelForCausalLM, OffloadedStaticCache

        >>> model = AutoModelForCausalLM.from_pretrained("openai-community/gpt2")
        >>> tokenizer = AutoTokenizer.from_pretrained("openai-community/gpt2")

        >>> inputs = tokenizer(text="My name is GPT2", return_tensors="pt")

        >>> # Prepare a cache class and pass it to model's forward
        >>> # Leave empty space for 10 new tokens, which can be used when calling forward iteratively 10 times to generate
        >>> max_generated_length = inputs.input_ids.shape[1] + 10
        >>> past_key_values = OffloadedStaticCache(config=model.config, max_batch_size=1, max_cache_len=max_generated_length, device=model.device, dtype=model.dtype)
        >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
        >>> past_kv_length = outputs.past_key_values # access cache filled with key/values from generation
        ```
    """

    is_compileable = True

    def __init__(
        self,
        config: PretrainedConfig,
        max_batch_size: int,
        max_cache_len: Optional[int],
        device: Union[str, torch.device],
        dtype: Optional[torch.dtype] = None,
        offload_device: Union[str, torch.device] = torch.device("cpu"),
        layer_device_map: Optional[dict[int, Union[str, torch.device, int]]] = None,
        tp_size: Optional[int] = None,
    ) -> None:
        super(Cache, self).__init__()

        # TODO (joao): to enable this cache on multiple devicesuse the pattern from `OffloadedCache`, which keeps
        # track of the original device of each layer
        unique_devices = set(layer_device_map.values()) if layer_device_map else set()
        if len(unique_devices) > 1:
            raise ValueError(f"OffloadedStaticCache does not support multiple devices. Got devices: {unique_devices}")

        self.max_batch_size = max_batch_size
        self.max_cache_len = config.max_position_embeddings if max_cache_len is None else max_cache_len
        self.device = torch.device(device) if layer_device_map is None else torch.device(layer_device_map[0])
        self.offload_device = torch.device(offload_device)
        self._dtype = dtype if dtype is not None else torch.float32

        # Some model define a custom `head_dim` != config.hidden_size // config.num_attention_heads
        head_dim = config.head_dim if hasattr(config, "head_dim") else config.hidden_size // config.num_attention_heads

        num_key_value_heads = (
            config.num_attention_heads
            if getattr(config, "num_key_value_heads", None) is None
            else config.num_key_value_heads
        )
        if tp_size is not None and tp_size > 1:
            if num_key_value_heads % tp_size != 0:
                raise ValueError(
                    f"Number of key value heads {num_key_value_heads} must be divisible by tensor parallel size {tp_size}."
                )
            # If the model is using tensor parallelism, we need to adjust the number of heads accordingly.
            num_key_value_heads //= tp_size

        cache_shape = (max_batch_size, num_key_value_heads, self.max_cache_len, head_dim)

        # Create offloaded CPU tensors.
        self.key_cache: list[torch.Tensor] = []
        self.value_cache: list[torch.Tensor] = []

        for i in range(config.num_hidden_layers):
            # First layer is always on-device.
            device = self.device if i == 0 else self.offload_device

            key_cache, value_cache = self._create_key_value_cache_tensors(cache_shape, device)

            self.key_cache.append(key_cache)
            self.value_cache.append(value_cache)

        # Create device tensors.
        self._device_key_cache: list[torch.Tensor] = []
        self._device_value_cache: list[torch.Tensor] = []

        for i in range(2):
            key_cache, value_cache = self._create_key_value_cache_tensors(cache_shape, self.device)

            self._device_key_cache.append(key_cache)
            self._device_value_cache.append(value_cache)

        # Create new CUDA stream for parallel prefetching.
        self._prefetch_stream = torch.cuda.Stream() if self.device.type == "cuda" else None

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Updates the cache with the new `key_states` and `value_states` for the layer `layer_idx`.
        It is VERY important to index using a tensor, otherwise you introduce a copy to the device.

        Parameters:
            key_states (`torch.Tensor`):
                The new key states to cache.
            value_states (`torch.Tensor`):
                The new value states to cache.
            layer_idx (`int`):
                The index of the layer to cache the states for.
            cache_kwargs (`dict[str, Any]`, *optional*):
                Additional arguments for the cache subclass. The `OffloadedStaticCache` needs the
                `cache_position` input to know how where to write in the cache.

        Return:
            A tuple containing the updated key and value states.
        """

        key_states = key_states.to(self.key_cache[layer_idx].dtype)
        value_states = value_states.to(self.value_cache[layer_idx].dtype)

        if layer_idx == 0:
            # Always there.
            k_out = self.key_cache[0]
            v_out = self.value_cache[0]
        else:
            # Wait for prefetch stream.
            if self._prefetch_stream is not None:
                torch.cuda.default_stream(self.device).wait_stream(self._prefetch_stream)

            k_out = self._device_key_cache[layer_idx & 1]
            v_out = self._device_value_cache[layer_idx & 1]

        self._prefetch_layer(layer_idx + 1)

        cache_position = cache_kwargs.get("cache_position") if cache_kwargs is not None else None
        if cache_position is None:
            k_out.copy_(key_states)
            v_out.copy_(value_states)

            # Copy the values to the offloaded device as well.
            if layer_idx == 0:
                self.key_cache[layer_idx].copy_(key_states.to(self.offload_device))
                self.value_cache[layer_idx].copy_(value_states.to(self.offload_device))
        else:
            # Note: here we use `tensor.index_copy_(dim, index, tensor)` that is equivalent to
            # `tensor[:, :, index] = tensor`, but the first one is compile-friendly and it does
            # explicitly an in-place operation, that avoids copies and uses less memory.
            try:
                k_out.index_copy_(2, cache_position, key_states)
                v_out.index_copy_(2, cache_position, value_states)
            except NotImplementedError:
                # The operator 'aten::index_copy.out' is not currently implemented for the MPS
                # device.
                k_out[:, :, cache_position] = key_states
                v_out[:, :, cache_position] = value_states

            # Copy the values to the offloaded device as well.
            if layer_idx != 0:
                cache_position = cache_position.to(self.offload_device)
                key_states = key_states.to(self.offload_device)
                value_states = value_states.to(self.offload_device)

                try:
                    self.key_cache[layer_idx].index_copy_(2, cache_position, key_states)
                    self.value_cache[layer_idx].index_copy_(2, cache_position, value_states)
                except NotImplementedError:
                    # The operator 'aten::index_copy.out' is not currently implemented for the MPS
                    # device.
                    self.key_cache[layer_idx][:, :, cache_position] = key_states
                    self.value_cache[layer_idx][:, :, cache_position] = value_states

        return k_out, v_out

    def get_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states. A layer index can be optionally passed."""
        is_empty_layer = (
            len(self.key_cache) == 0  # no cache in any layer
            or len(self.key_cache) <= layer_idx  # hasn't run a layer with cache after it
            or not self.key_cache[layer_idx].numel()  # the layer has no cache
        )
        layer_seq_length = self.key_cache[layer_idx].shape[-2] if not is_empty_layer else 0
        return layer_seq_length

    def get_max_cache_shape(self) -> Optional[int]:
        """Returns the maximum sequence length of the cached states."""

        return self.max_cache_len

    def reset(self) -> None:
        """Resets the cache values while preserving the objects."""

        # Zero out cache.
        for layer_idx in range(len(self.key_cache)):
            # In-place ops prevent breaking the static address.
            self.key_cache[layer_idx].zero_()
            self.value_cache[layer_idx].zero_()

    def _create_key_value_cache_tensors(
        self, shape: tuple[int, ...], device: torch.device
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Creates K/V cache tensors on a device. Pins memory for CPU tensors. Marks them as static
        addresses for non-CPU tensors.

        Args:
            shape (`tuple[int, ...]`): Shape.
            device (`torch.device`): Device.

        Returns:
            Key and value cache tensors as a tuple.
        """

        is_cpu_device = device == torch.device("cpu")

        key_cache = torch.zeros(shape, dtype=self._dtype, device=device, pin_memory=is_cpu_device)
        value_cache = torch.zeros(shape, dtype=self._dtype, device=device, pin_memory=is_cpu_device)

        # Note: `mark_static_address` is used to tag the cache as a fixed data pointer,
        # preventing compiled graph breaks when updating the cache.
        torch._dynamo.mark_static_address(key_cache)
        torch._dynamo.mark_static_address(value_cache)

        return key_cache, value_cache

    def _prefetch_layer(self, layer_idx: int) -> None:
        """Prefetch a layer to the device. Needs to be called in order of layer indices."""

        # Don't fetch layers that do not exist.
        if layer_idx >= len(self.key_cache):
            return

        # Alternate between two on-device caches.
        if self._prefetch_stream is not None:
            with torch.cuda.stream(self._prefetch_stream):
                self._prefetch_layer_in_context(layer_idx)
        else:
            self._prefetch_layer_in_context(layer_idx)

    def _prefetch_layer_in_context(self, layer_idx: int) -> None:
        """Performs the actual copy of the layer to device cache."""

        self._device_key_cache[layer_idx & 1].copy_(self.key_cache[layer_idx], non_blocking=True)
        self._device_value_cache[layer_idx & 1].copy_(self.value_cache[layer_idx], non_blocking=True)


# TODO (manuel, joao): remove this class, it is here only for backwards compatibility
# PEP 562: Lazy loading for deprecated location of MambaCache
def __getattr__(name: str) -> Any:
    if name == "MambaCache":
        warnings.warn(
            (
                "Importing `MambaCache` from `transformers.cache_utils` is deprecated and will be removed "
                "in a future version. Please import it from `transformers` or `transformers.models.mamba.cache_mamba` instead."
            ),
            FutureWarning,
            stacklevel=2,
        )

        class MambaCache:
            """
            Importing `MambaCache` from `transformers.cache_utils` is deprecated and will be removed
            in a future version. Please import it from `transformers` or `transformers.models.mamba.cache_mamba` instead.

            Cache for mamba model which does not have attention mechanism and key value states.

            Arguments:
                config (`PretrainedConfig):
                    The configuration file defining the shape-related attributes required to initialize the static cache.
                max_batch_size (`int`):
                    The maximum batch size with which the model will be used. Note that a new instance must be instantiated if a smaller batch size is used.
                dtype (`torch.dtype`, *optional*, defaults to `torch.float16`):
                    The default `dtype` to use when initializing the layer.
                device (`torch.device` or `str`, *optional*):
                    The device on which the cache should be initialized. Should be the same as the layer.

            Example:

                ```python
                >>> from transformers import AutoTokenizer, MambaForCausalLM, MambaCache

                >>> model = MambaForCausalLM.from_pretrained("state-spaces/mamba-130m-hf")
                >>> tokenizer = AutoTokenizer.from_pretrained("state-spaces/mamba-130m-hf")

                >>> inputs = tokenizer(text="My name is Mamba", return_tensors="pt")

                >>> # Prepare a cache class and pass it to model's forward
                >>> past_key_values = MambaCache(config=model.config, max_batch_size=1, device=model.device, dtype=model.dtype)
                >>> outputs = model(**inputs, past_key_values=past_key_values, use_cache=True)
                >>> outputs.past_key_values
                MambaCache()
                ```
            """

            is_compileable = True

            # TODO (joao): add layer_device_map arg and update code in `generate` accordingly
            def __init__(
                self,
                config,
                max_batch_size: int,
                dtype: torch.dtype = torch.float16,
                device: Union[torch.device, str, None] = None,
            ):
                self.max_batch_size = max_batch_size
                self._dtype = dtype
                self.intermediate_size = config.intermediate_size
                self.ssm_state_size = config.state_size
                self.conv_kernel_size = config.conv_kernel

                self.conv_states: list[torch.Tensor] = []
                self.ssm_states: list[torch.Tensor] = []
                device = torch.device(device) if device is not None else None
                for _ in range(config.num_hidden_layers):
                    conv_state: torch.Tensor = torch.zeros(
                        self.max_batch_size,
                        self.intermediate_size,
                        self.conv_kernel_size,
                        device=device,
                        dtype=self._dtype,
                    )
                    ssm_state: torch.Tensor = torch.zeros(
                        self.max_batch_size,
                        self.intermediate_size,
                        self.ssm_state_size,
                        device=device,
                        dtype=self._dtype,
                    )

                    torch._dynamo.mark_static_address(conv_state)
                    torch._dynamo.mark_static_address(ssm_state)
                    self.conv_states.append(conv_state)
                    self.ssm_states.append(ssm_state)

            def update_conv_state(
                self, layer_idx: int, new_conv_state: torch.Tensor, cache_position: torch.LongTensor
            ) -> torch.Tensor:
                # This `if` blocks is only reached in multigpu and if `layer_device_map` is not passed. It is used
                # when the cache is initialized in the forward pass (e.g. Mamba)
                if self.conv_states[layer_idx].device != new_conv_state.device:
                    self.conv_states[layer_idx] = self.conv_states[layer_idx].to(new_conv_state.device)

                conv_state = self.conv_states[layer_idx]
                cache_position = cache_position.clamp(0, self.conv_kernel_size - 1)

                conv_state = conv_state.roll(shifts=-1, dims=-1)
                conv_state[:, :, cache_position] = new_conv_state.to(device=conv_state.device, dtype=conv_state.dtype)
                self.conv_states[layer_idx].zero_()
                self.conv_states[layer_idx] += conv_state
                return self.conv_states[layer_idx]

            def update_ssm_state(self, layer_idx: int, new_ssm_state: torch.Tensor):
                self.ssm_states[layer_idx] = new_ssm_state.to(self.ssm_states[layer_idx].device)
                return self.ssm_states[layer_idx]

            def reset(self):
                for layer_idx in range(len(self.conv_states)):
                    # In-place ops prevent breaking the static address
                    self.conv_states[layer_idx].zero_()
                    self.ssm_states[layer_idx].zero_()

        return MambaCache
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
