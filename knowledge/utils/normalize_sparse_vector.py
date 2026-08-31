"""
Sparse vector normalization utilities.

Milvus SPARSE_FLOAT_VECTOR fields use dictionaries like
{dimension_id: weight}. This helper follows the course document and applies
L2 normalization to the sparse vector values.
"""

import numpy as np


def normalize_sparse_vector(sparse_vec):
    """
    Apply L2 normalization to a sparse vector.

    Args:
        sparse_vec: Raw sparse vector in dict format: {dimension: value}.

    Returns:
        L2-normalized sparse vector.
    """
    if not sparse_vec:
        return sparse_vec

    values = np.array(list(sparse_vec.values()), dtype=np.float64)

    l2_norm = np.linalg.norm(values)
    if l2_norm < 1e-9:
        return sparse_vec

    normalized_values = values / l2_norm

    return dict(zip(sparse_vec.keys(), normalized_values))
