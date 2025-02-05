import numpy as np
from scipy.sparse import csc_array, sparray
from scipy.sparse.csgraph import reverse_cuthill_mckee
from numba import njit, prange
import numpy as np

def expand_dependencies(shape_in, shape_out, dependencies):
    """
    Expand a given set of dependencies into a uniform list of tuples in the 
    PyMRM dependency notation, fully expanded.

    PyMRM Dependency Notation:
    --------------------------
    (index_in, index_out, fixed_axes_list, periodic_axes_list)

    - index_in: A tuple representing the position in the input (field) array that contributes
      to the value at index_out.
    - index_out: A tuple representing the position in the output (function) array,
      or None if no reference is needed (e.g., simple stencil).
    - fixed_axes_list: A list of axis indices that are considered fixed.
    - periodic_axes_list: A list of axis indices that are considered periodic.

    Returns:
    --------
    A list of tuples in the form (index_in, index_out, fixed_axes_list, periodic_axes_list)
    with all indices fully expanded into integers.
    """

    # Helper functions
    def slice_to_list(slc, dim_size):
        return list(range(*slc.indices(dim_size)))

    def expand_axis(axis_val, dim_size):
        """
        Expand a single axis value which could be:
        - int
        - slice
        - list of ints/slices
        - range
        """
        if isinstance(axis_val, int):
            # Single integer, no expansion needed
            return [axis_val]
        elif isinstance(axis_val, slice):
            # Expand slice
            return slice_to_list(axis_val, dim_size)
        elif isinstance(axis_val, list):
            # Could be a list of integers or slices
            expanded_list = []
            for v in axis_val:
                if isinstance(v, int):
                    expanded_list.append(v)
                elif isinstance(v, slice):
                    expanded_list.extend(slice_to_list(v, dim_size))
                elif isinstance(v, range):
                    expanded_list.extend(list(v))
                else:
                    raise ValueError("Unsupported element in list for axis expansion: {}".format(type(v)))
            return expanded_list
        elif isinstance(axis_val, range):
            return list(axis_val)
        else:
            raise ValueError("Unsupported type in axis specification: {}".format(type(axis_val)))

    def expand_index(idx, shape):
        """
        Expand a single index tuple. The index can contain ints, slices, lists, or ranges.
        Returns a list of fully expanded tuples, or [None] if idx is None.
        """
        if not isinstance(idx, tuple):
            raise ValueError("Index must be a tuple or None.")

        expanded_dims = []
        for i, val in enumerate(idx):
            expanded_dims.append(expand_axis(val, shape[i]))

        # Cartesian product of all expanded dimensions
        from itertools import product
        return list(product(*expanded_dims))

    # Normalize dependencies to a list
    if isinstance(dependencies, tuple):
        dependencies = [dependencies]

    # Convert shorthand notation to full triple form
    normalized_deps = []
    for dep in dependencies:
        if not isinstance(dep, tuple):
            raise ValueError("Each dependency must be a tuple.")

        if len(dep) == 3 and isinstance(dep[0], tuple):
            # Almost in full, assuming no periodic axes
            index_in, index_out, fixed_axes = dep
            periodic_axes = []
        elif len(dep) == 4 and isinstance(dep[0], tuple):
            # Already in full form
            index_in, index_out, fixed_axes, periodic_axes = dep
            for d in periodic_axes:
                if d in fixed_axes:
                    raise ValueError("Axis cannot be both fixed and periodic.")
                if (shape_in[d] != shape_out[d]):
                    raise ValueError("Periodic axes must have the same size in input and output shapes.")
        else:
            # Shorthand form (e.g., (0,1,0))
            index_in = dep
            index_out = (0,)*len(shape_out)
            fixed_axes = []
            periodic_axes = []

        # Ensure fixed_axes is a list
        if fixed_axes is None:
            fixed_axes = []
        elif not isinstance(fixed_axes, list):
            raise ValueError("fixed_axes_list must be a list or None.")
        
        # Ensure periodic_axes is a list
        if periodic_axes is None:
            periodic_axes = []
        elif not isinstance(periodic_axes, list):
            raise ValueError("periodic_axes_list must be a list or None.")

        normalized_deps.append((index_in, index_out, fixed_axes, periodic_axes))

    # Now expand reference and dependent indices
    expanded_deps = []
    for (idx_in, idx_out, fixed_axes, periodic_axes) in normalized_deps:
        if (idx_out==None):
            if len(fixed_axes) > 0:
                raise ValueError("Fixed axes are not allowed when the out-index is None.")
            idx_out = (0,)*len(shape_out)
        in_expanded = expand_index(idx_in, shape_in)    # list of tuples
        out_expanded = expand_index(idx_out, shape_out)    # list of tuples or [None]

        for idx_out in out_expanded:
            for idx_in in in_expanded:
                expanded_deps.append((idx_in, idx_out, fixed_axes, periodic_axes))

    return expanded_deps

@njit
def ravel_index_numba(shape, index):
    "Convert a multidimensional index to a flat index."
    lin_idx = 0
    for i in range(len(shape)):
        lin_idx = lin_idx * shape[i] + index[i]
    return lin_idx

@njit
def unravel_index_numba(lin_idx, shape):
    "Convert a flat (linear) index to a multidimensional index."
    idx = np.empty(len(shape), dtype=np.int64)
    for i in range(len(shape) - 1, -1, -1):
        idx[i] = lin_idx % shape[i]
        lin_idx //= shape[i]
    return idx

@njit
def iterate_over_entries(shape_in, shape_out, shape_rel, idx_in, idx_out, row_indices, col_indices, entry_idx):
    """
    Iterate over all valid relative entries defined by shape_rel and fill row_indices and col_indices.
    This function:
    - Interprets shape_rel as the size of the relative axes.
    - For each combination of relative indices (idx_rel), computes the absolute positions (idx_out, idx_in) by adding idx_rel to idx_out and idx_in and applying periodicity.
    - Stores the computed flat indices in row_indices and col_indices.

    Args:
        shape_in (np.ndarray): The full shape of the multidimensional input (field) array.
        shape_out (np.ndarray): The full shape of the multidimensional output (function) array.
        shape_rel (np.ndarray): Shape array representing the size of relative axes.
        idx_in (np.ndarray): Input position array (adjusted for relative indexing).
        idx_out (np.ndarray): Output position array (adjusted for relative indexing).
        row_indices (np.ndarray): Preallocated array for row indices.
        col_indices (np.ndarray): Preallocated array for column indices.
        entry_idx (int): Starting index in the row_indices/col_indices arrays to place data.

    Returns:
        int: The updated index after filling in entries.
    """
    num_dims = shape_in.size
    size_lin = 1
    for d in range(num_dims):
        size_lin *= shape_rel[d]

    out_idx = np.empty(num_dims, dtype=np.int64)
    in_idx = np.empty(num_dims, dtype=np.int64)

    current_idx = entry_idx
    for idx_lin in range(size_lin):
        idx_rel = unravel_index_numba(idx_lin, shape_rel)

        # Compute absolute indices for output and input
        for d in range(num_dims):
            # out position modulo shape
            out_idx[d] = (idx_out[d] + idx_rel[d]) % shape_out[d]
            # in position modulo shape
            in_idx[d] = (idx_in[d] + idx_rel[d]) % shape_in[d]

        # Convert multidimensional indices to linear form
        row_indices[current_idx] = ravel_index_numba(shape_out, out_idx)
        col_indices[current_idx] = ravel_index_numba(shape_in, in_idx)
        current_idx += 1

    return current_idx

def generate_sparsity_pattern(shape_in, shape_out, dependencies):
    """
    Generate row and column indices for the sparse matrix representation
    of a given stencil pattern. This function attempts to minimize loops
    over large dimensions by focusing on relative axes and using Numba-compiled
    helper functions for speed.

    Parameters:
    - shape_in: Tuple of ints, the shape of the multidimensional array corresponding to the field.
    - shape_out: Tuple of ints, the shape of the multidimensional array corresponding to the function.
    - dependencies: A list of tuples (idx_out, idx_in, fixed_axes, periodic_axes) representing the stencil pattern. Each dependency indicates how output positions relate to input positions and which axes are fixed or periodic.

    Returns:
    - row_indices: 1D numpy array of row indices for the sparse pattern.
    - col_indices: 1D numpy array of column indices for the sparse pattern.
    """
    shape_in = np.array(shape_in, dtype=np.int64)
    shape_out = np.array(shape_out, dtype=np.int64)
    num_dims = len(shape_in)
    if num_dims != len(shape_out):
        raise ValueError("Input and output shapes must have the same number of dimensions.")

    # Estimate total number of non-zero elements needed
    total_elements = 0
    for dep in dependencies:
        idx_in, idx_out, fixed_axes, periodic_axes = dep
        idx_in_arr = np.array(idx_in, dtype=np.int64)
        shape_rel = np.minimum(shape_in + np.minimum(-idx_in_arr,0),shape_out + np.minimum(idx_in_arr,0))
        for d in fixed_axes:
            shape_rel[d] = 1 
        for d in periodic_axes:  
            shape_rel[d] = np.minimum(shape_in[d], shape_out[d])
        shape_rel = np.maximum(shape_rel, 0)
        total_elements += np.prod(shape_rel)        

    # Preallocate arrays for row and col indices
    row_indices = np.empty(total_elements, dtype=np.int64)
    col_indices = np.empty(total_elements, dtype=np.int64)

    entry_index = 0
    for dep in dependencies:
        idx_in, idx_out, fixed_axes, periodic_axes = dep
        idx_in_arr = np.array(idx_in, dtype=np.int64)

        # Adjust shape_rel for periodic and fixed axes
        # Adjust idx_out and idx_in so that one of them starts at zero for relative indexing
        # This reduces complexity when computing final positions.
        idx_out_arr = np.maximum(-idx_in_arr,0)
        idx_in_arr = np.maximum(idx_in_arr,0)
        shape_rel = np.minimum(shape_in -idx_in_arr,shape_out - idx_out_arr)
        for d in fixed_axes:
            shape_rel[d] = 1
            idx_out_arr[d] = idx_out[d]
        for d in periodic_axes:
            shape_rel[d] = np.minimum(shape_in[d], shape_out[d])
        shape_rel = np.maximum(shape_rel, 0)  

        # Fill row and col indices using the helper
        entry_index = iterate_over_entries(shape_in, shape_out, shape_rel, idx_in_arr, idx_out_arr,
                                             row_indices, col_indices, entry_index)

    # Sort the indices by row and then by column for canonical form
    sorted_idx = np.unique(np.concatenate((col_indices.reshape((1, -1)), row_indices.reshape((1, -1))), axis=0), axis=1)
    col_indices = sorted_idx[0, :]
    row_indices = sorted_idx[1, :]
    
    return row_indices, col_indices

@njit
def group_columns_by_non_overlap_numba(indptr, indices):
    n = indptr.size - 1
    g = np.full(n, n, dtype=np.int64)
    groupnum = 0
    J = np.arange(n)
    while len(J) > 0:
        g[J[0]] = groupnum
        col = np.zeros(n, dtype=np.bool_)
        for i in range(indptr[J[0]], indptr[J[0]+1]):
            col[indices[i]] = True
        for k in J:
            if (not col[k]):
                for i in range(indptr[k], indptr[k+1]):
                    col[indices[i]] = True
                g[k] = groupnum
        J = np.where(g == n)[0]
        groupnum += 1
    return g, groupnum

def colgroup(*args, shape=None, try_reorder=True):
    if isinstance(args[0], sparray):
        S = csc_array(args[0])
        T = csc_array((S.data != 0, S.indices, S.indptr), shape=S.shape)
    elif isinstance(args[0], np.ndarray) and isinstance(args[1], np.ndarray):
        rows = args[0]
        cols = args[1]
        data = np.ones(rows.shape, dtype=np.bool_)
        T = csc_array((data, (rows, cols)), shape=shape)
        if (shape[0] != shape[1]):
            try_reorder = False
    else:
        raise ValueError("Input should be a sparse array, or two ndarrays containing row and col indices")    

    TT = T.transpose() @ T
    g, num_groups = group_columns_by_non_overlap_numba(TT.indptr, TT.indices)
    
    if try_reorder and num_groups > 1:   
    # Form the reverse column minimum-degree ordering.
        p = reverse_cuthill_mckee(T)
        p = p[::-1]
        T = T[:, p]
        TT = T.transpose() @ T
        g2, num_groups2 = group_columns_by_non_overlap_numba(TT.indptr, TT.indices)
        # Use whichever packing required fewer groups.
        if num_groups2 < num_groups:
            q = np.argsort(p)
            g = g2[q]
            num_groups = num_groups2
    
    return g, num_groups

def stencil_block_diagonals(ndims=1, axes_diagonals=[], axes_blocks=[-1], periodic_axes=[]):
    if (ndims < len(axes_diagonals) or ndims < len(axes_blocks)):
        raise ValueError("Number of dimensions should be greater than the number of axes.")
    dependencies = []
    dep_block = ndims*[0,]
    for axis in axes_blocks:
        dep_block[axis] = slice(None)
    if len(axes_diagonals) == 0:
        dep = (tuple(dep_block), tuple(dep_block), axes_blocks, periodic_axes)
        dependencies.append(dep)
    else:
        for axis in axes_diagonals:
            dep_diagonals = dep_block.copy()
            dep_diagonals[axis] = [-1,0,1]
            dep = (tuple(dep_diagonals), tuple(dep_block), axes_blocks, periodic_axes)
            dependencies.append(dep)
    return dependencies

def precompute_perturbations(c, dc, num_gr, gr):
    c_perturb = np.tile(c[np.newaxis, ...], (num_gr,) + (1,) * c.ndim)
    c_perturb.ravel()[c.size * gr.ravel() + np.arange(c.size)] += dc.ravel()
    return c_perturb

@njit(parallel=True)
def precompute_perturbations_numba(c, dc, num_gr, gr):
    c_flat = c.ravel()
    dc_flat = dc.ravel()
    gr_flat = gr.ravel()
    c_size = c_flat.size
    
    c_perturb_flat = np.empty((num_gr, c_size), dtype=c.dtype)
    
    for k in prange(num_gr):
        for idx in range(c_size):
            c_perturb_flat[k, idx] = c_flat[idx]
    
    for idx in prange(c_size):
        c_perturb_flat[gr_flat[idx], idx] += dc_flat[idx]
    
    c_perturb = c_perturb_flat.reshape((num_gr,) + c.shape)
    return c_perturb

@njit(parallel=True)
def compute_df(f_value, perturbed_values, num_gr):
    df = np.empty(perturbed_values.shape)
    for k in prange(num_gr):
        df[k, ...] = (perturbed_values[k, ...] - f_value)
    return df

#@njit(parallel=True)
def compute_df2(f, f_value, c_values, num_gr):
    df = np.empty((num_gr,*f_value.shape))
    for k in prange(num_gr):
        df[k, ...] = (f(c_values[k, ...]) - f_value)
    return df

class NumJac:
    def __init__(
        self,
        shape=None,
        shape_in=None,
        shape_out=None,
        stencil=stencil_block_diagonals,
        eps_jac=1e-6,
        **kwargs,
    ):
        """
        Initialize the NumJac class.

        Args:
            shape (tuple, optional): Shape of the multidimensional array (used when shape_in == shape_out).
            shape_in (tuple, optional): Shape of the input array (used when shape != shape_out).
            shape_out (tuple, optional): Shape of the output array (used when shape != shape_out).
            stencil (callable, optional): Function to generate the stencil. Default is None.
            eps_jac (float, optional): Perturbation size for numerical Jacobian. Default is 1e-6.
            *args: Additional positional arguments passed to the stencil function.
            **kwargs: Additional keyword arguments passed to the stencil function.

        Raises:
            ValueError: If both `shape` and `shape_in`/`shape_out` are specified or if inputs are inconsistent.
        """
        if shape is not None and (shape_in is not None or shape_out is not None):
            raise ValueError(
                "Specify either 'shape' or both 'shape_in' and 'shape_out', but not both."
            )

        if shape is not None:
            # Default case: shape_in == shape_out
            self.shape_in = shape
            self.shape_out = shape
        elif shape_in is not None and shape_out is not None:
            # General case: shape_in != shape_out
            if len(shape_in) != len(shape_out):
                raise ValueError("Input and output shapes must have the same number of dimensions.")
            self.shape_in = shape_in
            self.shape_out = shape_out
        else:
            raise ValueError("You must specify either 'shape' or both 'shape_in' and 'shape_out'.")

        self.eps_jac = eps_jac

        # Initialize stencil
        self.init_stencil(stencil, **kwargs)

    def init_stencil(self, stencil, **kwargs):
        """
        Initialize the stencil pattern.

        Args:
            stencil (callable): Function to generate the stencil.
            *args: Additional positional arguments passed to the stencil function.
            **kwargs: Additional keyword arguments passed to the stencil function.
        """
        if stencil is None:
            raise ValueError("A stencil function or stencil specification must be provided.")
        
        # Call the stencil function with ndims, *args, and **kwargs
        if callable(stencil):
            stencil = stencil(ndims=len(self.shape_in), **kwargs)
        self.dependencies = expand_dependencies(self.shape_in, self.shape_out, stencil)
        self.rows, self.cols = generate_sparsity_pattern(self.shape_in, self.shape_out, self.dependencies)
        self.gr, self.num_gr = colgroup(self.rows, self.cols, shape = (np.prod(self.shape_out), np.prod(self.shape_in)))
        
    def __call__(self, f, c):
        f_value = f(c)
        dc = -self.eps_jac * np.abs(c)
        dc[dc > (-self.eps_jac)] = self.eps_jac
        dc = (c + dc) - c
        
        # Precompute perturbations using Numba
        #c_perturb = precompute_perturbations_numba(c, dc, self.num_gr, self.gr)
        c_perturb = np.tile(c[np.newaxis, ...], (self.num_gr,) + (1,) * c.ndim)
        c_perturb.ravel()[c.size * self.gr.ravel() + np.arange(c.size)] += dc.ravel()
    
        # Evaluate function f on perturbed values
        # perturbed_values = np.array([f(c_perturb[k, ...]) for k in range(self.num_gr)])
        # Compute dfdc using Numba
        #df = compute_df(f_value, perturbed_values, self.num_gr)
        df = compute_df2(f, f_value, c_perturb, self.num_gr)
        
        values = df.reshape((self.num_gr, -1))[self.gr.ravel()[self.cols], self.rows]/dc.ravel()[self.cols]
        jac = csc_array((values, (self.rows, self.cols)), shape=(f_value.size, c.size))
        
        return f_value, jac