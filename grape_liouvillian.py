from typing import List, Union
import numpy as np
from scipy.linalg import expm
from tqdm import tqdm
from qutip import Qobj, liouvillian
from scipy.optimize import BFGS, line_search, minimize, OptimizeResult


def _liouvillian_operator_batch(H: np.ndarray, c_ops: List[np.ndarray]):
    """calculate liouvillian operator from Hamiltonian and collapse operators
    

    Args:
        H (np.ndarray): N*n*n matrix, N is the number of time steps, n is the dimension of the system
        c_ops (List[np.ndarray]): _description_
    """
    N, n, _ = np.shape(H)
    # l1 is the vectorized form of hamiltonian operator
    _l1 = -1j * (
        np.kron(np.eye(n), H) - np.kron(H.transpose((0, 2, 1)), np.eye(n))
        )
    # l2 is the vectorized form of lindblad collapse operators
    _l2 = 0
    for c_op in c_ops:
        _l2 += np.kron(c_op.conj(), c_op) \
                - 0.5 * np.kron(np.eye(n), c_op.conj().T @ c_op) \
                - 0.5 * np.kron((c_op.conj().T @ c_op).T, np.eye(n))
    _l = _l1 + _l2
    return _l1 + _l2
        
    


def _liouvillian_propagator(
    H0: np.ndarray,
    Hk: List[np.ndarray],
    c_ops: List[np.ndarray],
    dissipators: Union[List[np.ndarray], None],
    delta_t: float,
    u_kj: np.ndarray,
) -> np.ndarray:
    """calculate propagator in liouvillian form

    Args:
        H0 (np.ndarray): 
        Hk (List[np.ndarray]): 
        c_ops (List[np.ndarray]): 
        dissipators (Union[List[np.ndarray], None]): 
        delta_t (float): 
        u_kj (np.ndarray): 
    """
    m, N = np.shape(u_kj)
    n = H0.shape[0] 
    _Hk_sum = np.tensordot(u_kj, Hk, axes=([0], [0]))  # Nxnxn
    
    L = _liouvillian_operator_batch(H0 + _Hk_sum, c_ops) + 0 if dissipators is None else np.sum(dissipators)
    Lj = expm(delta_t * L)
    return Lj



def _liouvillian_density_matrix(Lj: np.ndarray, rho_0: Union[Qobj, np.ndarray]) -> np.ndarray:
    """_summary_

    Args:
        Lj (): _description_
        rho_0 (_type_): _description_
    """
    N, n2, _ = np.shape(Lj)
    rho_0 = np.array(rho_0).reshape((-1, 1))

    rhoj = np.ndarray((N, n2, 1), np.complex128)
    rhoj[0] = Lj[0] @ rho_0 
    for j in range(1, N):
        rhoj[j] = Lj[j] @ rhoj[j - 1]

    return rhoj


def _liouvillian_lambda(Lj: np.ndarray, C: Union[Qobj, np.ndarray]) -> np.ndarray:
    """_summary_

    Args:
        Lj (_type_): _description_
        C (_type_): _description_
    """
    N, n2, _ = np.shape(Lj)
    n = int(np.sqrt(n2))

    # ??? why I need to conjugate C here
    C = np.array(C).reshape((-1, 1))

    lambdaj = np.ndarray((N, n2, 1), np.complex128)
    lambdaj[-1] = C
    for j in range(N - 2, -1, -1):
        lambdaj[j] = Lj[j + 1].conj().T @ lambdaj[j + 1]

    return lambdaj


def _liouvillian_gradient(
    lambdaj: np.ndarray, 
    rhoj: np.ndarray, 
    delta_t: float, 
    Hk: List[Qobj]
) -> np.ndarray:
    """_summary_

    Args:
        lambdaj (_type_): _description_
        rhoj (_type_): _description_
        delta_t (_type_): _description_
        Hk (_type_): _description_
    """
    m = len(Hk)
    N, n2, _ = np.shape(rhoj)
    n = int(np.sqrt(n2))
    lambdaj = np.array(lambdaj)
    rhoj = np.array(rhoj)
    Hk = np.array(Hk)

    commutation = 1j * delta_t \
        * (
            np.matmul(Hk[:, None], rhoj.reshape((N, n, n))) \
            - np.matmul(rhoj.reshape((N, n, n)), Hk[:, None])
        ).reshape((m, N, n2, 1))
    lambdaj = lambdaj.conj().swapaxes(1, 2)
    ipmat = -np.matmul(lambdaj, commutation)
    um = np.trace(ipmat, axis1=2, axis2=3)
    # print("rhoj[0]:", rhoj[0])
    # print("lambdaj[0]:", lambdaj[0])
    # print("rhoj[-1]:", rhoj[-1])
    # print("lambdaj[-1]:", lambdaj[-1])
    # exit()

    return um


def grape_liouvillian_bfgs(
    u_0: np.ndarray, 
    rho_0: Qobj, 
    C: Qobj, 
    T: int, 
    H0: Qobj,
    Hk: List[Qobj], 
    c_ops: List[Qobj] = [],
    dissipators: Union[List[Qobj], None] = None,
    target: str = "trace_real", 
    max_iter:int = 1000,
    gtol: float = 1e-6,
    atol: float = 1e-6,
    disp: bool = True,
    method = "direct"
) -> OptimizeResult:
    """grape algorithm, using BFGS method from scipy

    Args:
        H0 (Union[np.ndarray, Qobj]): nxn matrix or a Qobj with same shape, basic Hamiltonian
        Hk (List[Union[np.ndarray, Qobj]]): list of nxn matrices or list of Qobj with same shape, control Hamiltonian
        u_0 (np.ndarray): mxN matrix u[k, j] is the k-th control function at time j
        rho_0 (Union[np.ndarray, Qobj]): nxn matrix or a Qobj with same shape, initial state
        C (Union[np.ndarray, Qobj]): final target operator
        T (int): final time
        c_ops (List[Union[np.ndarray, Qobj]]): list of nxn matrices or list of Qobj with same shape, collapse operators
        dissipators (Union[List[Union[np.ndarray, Qobj]], None]): list of nxn matrices or list of Qobj with same shape, other dissipators with liouvillian form
        target (str, optional): different evaluation function. Defaults to "trace_real". Options: ["trace_real", "trace_both", "abs"].
        max_iter (int, optional): maxium iteration number. Defaults to 1000.
        gtol (float, optional): BFGS options,  gradient tolerence. Defaults to 1e-6.
        disp (bool, optional): BFGS options, whether to print state to console. Defaults to True.

    Returns:
        OptimizeResult: result of optimization, note that x is a 1d array, need to reshape to (m, N)
    """
    # basic check
    m, N = u_0.shape
    assert m == len(Hk), "number of control functions must be equal to number of control Hamiltonians"
    delta_t = T / N
    if isinstance(rho_0, Qobj):
        rho_0 = rho_0.full()
    if isinstance(C, Qobj):
        C = C.full()
    
    print(C)
        
    assert target in ["trace_real", "trace_both", "abs"], "target function not supported"

    # check hamiltonian shape
    if isinstance(H0, Qobj):
        H0 = H0.full()
    n = H0.shape[0]
    assert H0.shape == (n, n), "basic Hamiltonian must be a square matrix"
    for i, H in enumerate(Hk):
        if isinstance(H, Qobj):
            Hk[i] = H.full()
        assert H.shape == (n, n), "control Hamiltonian must be a square matrix"
    
    for i, c_op in enumerate(c_ops):
        if isinstance(c_op, Qobj):
            c_ops[i] = c_op.full()
        assert c_op.shape == (n, n), "collapse operator must be a square matrix"
        
    # copy u_0
    u_kj = np.array(u_0)
    
    def _f(x):
        fx = np.trace(np.dot(
            C.T.conjugate(), 
            _liouvillian_density_matrix(
                _liouvillian_propagator(H0, Hk, c_ops, dissipators, delta_t, x.reshape(m, N)), 
                rho_0
            )[-1].reshape((n, n))
        )).real.astype(np.float64)
        return -1 * fx
    
    def _grad_f(x):
        _Lj = _liouvillian_propagator(H0, Hk, c_ops, dissipators, delta_t, x.reshape(m, N))
        _lambda_j = _liouvillian_lambda(_Lj, C)
        _rho_j = _liouvillian_density_matrix(_Lj, rho_0)
        grad = _liouvillian_gradient(_lambda_j, _rho_j, delta_t, Hk).flatten().real.astype(np.float64)
        return -1 * grad
    
    res = None
    if method == "direct" or method == "cascaded":
        descend_count = 0
        phi_old = 0
        for i in range(max_iter):
            phi = _f(u_kj.flatten())
            grad = _grad_f(u_kj.flatten()).reshape((m, N)).real.astype(np.float64)
            
            u_kj = u_kj - 10 * grad
            
            if np.abs(phi - phi_old) < atol:
                break
            if phi > phi_old:
                descend_count += 1
                if descend_count > 5:
                    break
            phi_old = phi
            print(phi)
        res = u_kj
    if method.lower() == "bfgs" or method == "cascaded":
        res = minimize(
            _f,
            u_kj.flatten(),
            method="BFGS",
            jac=_grad_f,
            options={
                "gtol": gtol,
                "disp": disp,
                "maxiter": max_iter,
            }
        )

    return res