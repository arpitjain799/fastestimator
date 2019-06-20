import json
import os

import PIL.Image
import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from tensorflow.python import keras

from fastestimator.util.saliencies import GradientSaliency, IntegratedGradients
from fastestimator.util.util import is_number


def show_text(axis, background, text, title=None):
    """
    Plots a given image onto an axis

    Args:
        axis: The matplotlib axis to plot on
        background: A background image to display behind the text (useful for sizing the plot correctly)
        text: The text to display
        title: A title for the image
    """
    axis.axis('off')
    axis.imshow(background, cmap=plt.get_cmap(name="Greys_r"), vmin=0, vmax=1)
    axis.text(0.5, 0.5, text,
              ha='center', va='center', transform=axis.transAxes, wrap=False, family='monospace')
    if title is not None:
        axis.set_title(title)


def show_image(axis, im, title=None):
    """
    Plots a given image onto an axis

    Args:
        axis: The matplotlib axis to plot on
        im: The image to display (width X height)
        title: A title for the image
    """
    axis.axis('off')
    im = ((np.asarray(im) + 1) * 127.5).astype(np.uint8)
    axis.imshow(im)
    if title is not None:
        axis.set_title(title)


def show_gray_image(axis, im, title=None, color_map="inferno"):
    """
    Plots a given image onto an axis

    Args:
        axis: The matplotlib axis to plot on
        im: The image to display (width X height)
        title: A title for the image
        color_map: The color set to be used (since the image is gray scale)
    """
    axis.axis('off')
    axis.imshow(im, cmap=plt.get_cmap(name=color_map), vmin=0, vmax=1)
    if title is not None:
        axis.set_title(title)


def load_image(file_path, strip_alpha=False):
    """
    Args:
        file_path: The path to an image file
        strip_alpha: True to convert an RGBA image to RGB
    Returns:
        The image loaded into memory and scaled to a range of [-1, 1]
    """
    im = PIL.Image.open(file_path)
    if strip_alpha and im.mode == "RGBA":
        background = PIL.Image.new("RGB", im.size, (0, 0, 0))
        background.paste(im, mask=im.split()[3])
        im = background
    im = np.asarray(im)
    return im / 127.5 - 1.0


def load_dict(dict_path):
    """
    Args:
        dict_path: The path to a json dictionary
    Returns:
        A dictionary corresponding to the info from the file. If the file was formatted with arrays as the values for a
         key, the last element of the array is used as the value for the key in the parsed dictionary
    """
    parsed = None
    if dict_path is not None:
        with open(dict_path) as f:
            parsed = json.load(f)
            for key in parsed:
                entry = parsed[key]
                if type(entry) == list:
                    parsed[key] = parsed[key][-1]
    return parsed


@tf.function
def compute_percentile(tensor, percentile, keepdims=True):
    """
    Args:
        tensor: A tensor with batches on the zero axis. Shape (batch X ...)
        percentile: The percentile value to be computed for each batch in the tensor
        keepdims: Whether to keep shape compatibility with the input tensor
    Returns:
        A tensor corresponding to the given percentile value within each batch of the input tensor
    """
    result = tf.reduce_min(tf.math.top_k(tf.reshape(tensor, (tensor.shape[0], -1)),
                                         tf.cast(
                                             tf.math.ceil((1 - percentile / 100) * tensor.shape[1] * tensor.shape[2]),
                                             tf.int32), sorted=False).values, axis=1)
    if keepdims:
        result = tf.reshape(result, [tensor.shape[0]] + [1 for _ in tensor.shape[1:]])
    return result


@tf.function
def convert_for_visualization(batched_masks, percentile=99):
    """
    Args:
        batched_masks: Input masks, channel values to be reduced by absolute value summation
        percentile: The percentile [0-100] used to set the max value of the image
    Returns:
        A (batch X width X height) image after visualization clipping is applied
    """
    flattened_mask = tf.reduce_sum(tf.abs(batched_masks), axis=3)

    vmax = compute_percentile(flattened_mask, percentile)
    vmin = tf.reduce_min(flattened_mask, axis=(1, 2), keepdims=True)

    return tf.clip_by_value((flattened_mask - vmin) / (vmax - vmin), 0, 1)


def decode_predictions(predictions, top=3, dictionary=None):
    """
    Args:
        predictions: A batched numpy array of class prediction scores (Batch X Predictions)
        top: How many of the highest predictions to capture
        dictionary: {"<class_idx>" -> "<class_name>"}
    Returns:
        A right-justified newline-separated array of the top classes and their associated probabilities.
        There is one entry in the results array per batch in the input
    """
    results = []
    for prediction in predictions:
        top_indices = prediction.argsort()[-top:][::-1]
        if dictionary is None:
            result = ["Class {:d}: {:.4f}".format(i, prediction[i]) for i in top_indices]
        else:
            result = ["{:s}: {:.4f}".format(dictionary[str(i)], prediction[i]) for i in top_indices]
        max_width = len(max(result, key=lambda s: len(s)))
        result = str.join("\n", [s.rjust(max_width) for s in result])
        results.append(result)
    return results


def interpret_model(model, model_input, baseline_input=None, decode_dictionary=None, color_map="inferno", smooth=7,
                    save=False, save_path='.'):
    """ Displays or saves a saliency mask interpretation of the given input
    Args:
        model: A model to evaluate. Should be a classifier which takes the 0th axis as the batch axis
        model_input: Input tensor, shaped for the model ex. (1, 299, 299, 3)
        baseline_input: An example of what a blank model input would be.
                        Should be a tensor with the same shape as model_input
        decode_dictionary: A dictionary of "class_idx" -> "class_name" associations
        color_map: The color map to use to visualize the saliency maps.
                        Consider "Greys_r", "plasma", or "magma" as alternatives
        smooth: The number of samples to use when generating a smoothed image
        save: Whether to save (True) or display (False) the result
        save_path: Where to save the output if save=True
    """
    predictions = np.asarray(model(model_input))
    decoded = decode_predictions(predictions, top=3, dictionary=decode_dictionary)

    grad_sal = GradientSaliency(model)
    grad_int = IntegratedGradients(model)

    vanilla_masks = grad_sal.get_mask(model_input)
    vanilla_ims = convert_for_visualization(vanilla_masks)
    smooth_masks = grad_sal.get_smoothed_mask(model_input, nsamples=smooth)
    smooth_ims = convert_for_visualization(smooth_masks)
    smooth_integrated_masks = grad_int.get_smoothed_mask(model_input, nsamples=smooth, input_baseline=baseline_input)
    smooth_integrated_ims = convert_for_visualization(smooth_integrated_masks)

    filtered_inputs = (model_input + 1) * np.asarray(smooth_integrated_ims)[:, :, :, None] - 1

    num_rows = model_input.shape[0]
    num_cols = 6
    dpi = 96.0
    fig, axs = plt.subplots(num_rows, num_cols,
                            figsize=(num_cols * (model_input.shape[2] / dpi), num_rows * (model_input.shape[1] / dpi)),
                            dpi=dpi)
    if num_rows == 1:
        axs = [axs]  # axs object not wrapped if there's only one row
    for i in range(num_rows):
        show_text(axs[i][0], np.ones_like(model_input[i]), decoded[i], title="Predictions" if i == 0 else None)
        show_image(axs[i][1], model_input[i], title="Raw" if i == 0 else None)
        show_image(axs[i][2], filtered_inputs[i], title="Filtered" if i == 0 else None)
        show_gray_image(axs[i][3], vanilla_ims[i], color_map=color_map, title="Vanilla" if i == 0 else None)
        show_gray_image(axs[i][4], smooth_ims[i], color_map=color_map, title="Smoothed" if i == 0 else None)
        show_gray_image(axs[i][5], smooth_integrated_ims[i], color_map=color_map,
                        title="Integrated Smoothed" if i == 0 else None)
    plt.subplots_adjust(top=0.95, bottom=0.01, left=0.01, right=0.99, hspace=0.03, wspace=0.03)
    # plt.tight_layout(pad=0.3, h_pad=0.03, w_pad=0.03, rect=(0, 0, 0.98, 0.98))

    if not save:
        plt.show()
    else:
        os.makedirs(save_path, exist_ok=True)
        save_file = os.path.join(save_path, 'saliency.png')
        print("Saving to %s" % save_file)
        plt.savefig(save_file, dpi=300, bbox_inches="tight")


def load_and_interpret(model_path, input_paths, baseline=-1, input_type='float32', dictionary_path=None,
                       strip_alpha=False, smooth_factor=7, save=False, save_dir=None):
    """ A helper class to load input and invoke the interpretation api

    Args:
        model_path: The path the model file (str)
        input_paths: The paths to model input files [(str),...]
        baseline: Either a number corresponding to the baseline for integration, or a path to a baseline file
        input_type: The data type of the model inputs, ex 'float32'
        dictionary_path: The path to a dictionary file encoding a 'class_idx'->'class_name' mapping
        strip_alpha: Whether to collapse alpha channels when loading an input (bool)
        smooth_factor: How many iterations of the smoothing algorithm to run (int)
        save: Whether to save (True) or display (False) the resulting image
        save_dir: Where to save the image if save=True
    """
    model_dir = os.path.dirname(model_path)
    if save_dir is None:
        save_dir = model_dir
    network = keras.models.load_model(model_path)
    dic = load_dict(dictionary_path)
    inputs = [load_image(input_paths[i], strip_alpha=strip_alpha) for i in range(len(input_paths))]
    max_shapes = np.maximum.reduce([inp.shape for inp in inputs], axis=0)
    tf_image = tf.stack([tf.image.resize_with_crop_or_pad(tf.convert_to_tensor(im, dtype=input_type), max_shapes[0],
                                                          max_shapes[1]) for im in inputs], axis=0)
    if is_number(baseline):
        baseline_gen = tf.constant_initializer(float(baseline))
        baseline_image = baseline_gen(shape=tf_image.shape, dtype=input_type)
    else:
        baseline_image = load_image(baseline)
        baseline_image = tf.convert_to_tensor(baseline_image, dtype=input_type)

    interpret_model(network, tf_image, baseline_input=baseline_image, decode_dictionary=dic, smooth=smooth_factor,
                    save=save, save_path=save_dir)
