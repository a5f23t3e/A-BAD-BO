import os
import math
from dataclasses import dataclass

import torch
from botorch.acquisition import qExpectedImprovement, UpperConfidenceBound
from botorch.fit import fit_gpytorch_mll
from botorch.generation import MaxPosteriorSampling
from botorch.models import SingleTaskGP
from botorch.optim import optimize_acqf
from botorch.test_functions import Ackley
from botorch.utils.transforms import unnormalize
from torch.quasirandom import SobolEngine

import gpytorch
from gpytorch.constraints import Interval
from gpytorch.kernels import MaternKernel, ScaleKernel
from gpytorch.likelihoods import GaussianLikelihood
from gpytorch.mlls import ExactMarginalLogLikelihood
from gpytorch.priors import HorseshoePrior


device = torch.device("cpu")
dtype = torch.double
SMOKE_TEST = os.environ.get("SMOKE_TEST")

# fun = Ackley(dim=20, negate=True).to(dtype=dtype, device=device)
# fun.bounds[0, :].fill_(-5)
# fun.bounds[1, :].fill_(10)
# dim = fun.dim
# lb, ub = fun.bounds

batch_size = 4
# n_init = 2 * dim
# max_cholesky_size = float("inf")  # Always use Cholesky


# def eval_objective(x):
#     """This is a helper function we use to unnormalize and evalaute a point"""
#     return fun(unnormalize(x, fun.bounds))

@dataclass
class TurboState:
    dim: int
    batch_size: int
    length: float = 0.5
    length_min: float = 0.5**4
    length_max: float =  1.0 # original 1.6
    failure_counter: int = 0
    failure_tolerance: int = float("nan")  # Note: Post-initialized
    success_counter: int = 0
    success_tolerance: int = 3  # Note: The original paper uses 3
    best_value: float = -float("inf")
    restart_triggered: bool = False

    def __post_init__(self):
        self.failure_tolerance = math.ceil(
            max([4.0 / self.batch_size, float(self.dim) / self.batch_size])
        )
        self.failure_tolerance = 20


def update_state(state, Y_next):
    if max(Y_next) > state.best_value + 1e-3 * math.fabs(state.best_value):
        state.success_counter += 1
        state.failure_counter = 0
    else:
        state.success_counter = 0
        state.failure_counter += 1

    if state.success_counter == state.success_tolerance:  # Expand trust region
        state.length = min(2.0 * state.length, state.length_max)
        state.success_counter = 0
    elif state.failure_counter == state.failure_tolerance:  # Shrink trust region
        state.length /= 2.0
        state.failure_counter = 0

    state.best_value = max(state.best_value, max(Y_next).item())
    if state.length < state.length_min:
        state.restart_triggered = True
    return state

def generate_batch(
    state,
    model,  # GP model
    X,  # Evaluated points on the domain [0, 1]^d
    Y,  # Function values
    batch_size,
    n_candidates=None,  # Number of candidates for Thompson sampling
    num_restarts=10,
    raw_samples=20,
    acqf="ts",  # "ei" or "ts"
):
    assert acqf in ("ts", "ei", "gp-ucb")
    assert X.min() >= 0.0 and X.max() <= 1.0 and torch.all(torch.isfinite(Y))
    if n_candidates is None:
        n_candidates = min(5000, max(2000, 200 * X.shape[-1]))

    # Scale the TR to be proportional to the lengthscales
    x_center = X[Y.argmax(), :].clone()
    weights = model.covar_module.base_kernel.lengthscale.squeeze().detach()
    weights = weights / weights.mean()
    weights = weights / torch.prod(weights.pow(1.0 / len(weights)))
    tr_lb = torch.clamp(x_center - weights * state.length / 2.0, 0.0, 1.0)
    tr_ub = torch.clamp(x_center + weights * state.length / 2.0, 0.0, 1.0)

    if acqf == "ts":
        dim = X.shape[-1]
        sobol = SobolEngine(dim, scramble=True)
        pert = sobol.draw(n_candidates).to(dtype=dtype, device=device)
        pert = tr_lb + (tr_ub - tr_lb) * pert

        # Create a perturbation mask
        prob_perturb = min(20.0 / dim, 1.0)
        mask = torch.rand(n_candidates, dim, dtype=dtype, device=device) <= prob_perturb
        ind = torch.where(mask.sum(dim=1) == 0)[0]
        mask[ind, torch.randint(0, dim - 1, size=(len(ind),), device=device)] = 1

        # Create candidate points from the perturbations and the mask
        X_cand = x_center.expand(n_candidates, dim).clone()
        X_cand[mask] = pert[mask]

        # Sample on the candidate points
        thompson_sampling = MaxPosteriorSampling(model=model, replacement=False)
        with torch.no_grad():  # We don't need gradients when using TS
            X_next = thompson_sampling(X_cand, num_samples=batch_size)

    elif acqf == "ei":
        ei = qExpectedImprovement(model, Y.max())
        X_next, acq_value = optimize_acqf(
            ei,
            bounds=torch.stack([tr_lb, tr_ub]),
            q=batch_size,
            num_restarts=num_restarts,
            raw_samples=raw_samples,
        )
        
    elif acqf == "gp-ucb":
        ucb = UpperConfidenceBound(model, beta=0.5)
        X_next, acq_value = optimize_acqf(
            ucb,
            bounds=torch.stack([tr_lb, tr_ub]),
            q=batch_size,
            num_restarts=num_restarts,
            raw_samples=raw_samples,
        )

    return X_next

# def get_initial_points(dim, n_pts, seed=0):
#     sobol = SobolEngine(dimension=dim, scramble=True, seed=seed)
#     X_init = sobol.draw(n=n_pts).to(dtype=dtype, device=device)
#     return X_init

# X_turbo = get_initial_points(dim, n_init)
# Y_turbo = torch.tensor(
#     [eval_objective(x) for x in X_turbo], dtype=dtype, device=device
# ).unsqueeze(-1)

# state = TurboState(dim, batch_size=batch_size)

# NUM_RESTARTS = 10 if not SMOKE_TEST else 2
# RAW_SAMPLES = 512 if not SMOKE_TEST else 4
# N_CANDIDATES = min(5000, max(2000, 200 * dim)) if not SMOKE_TEST else 4

# torch.manual_seed(0)

# while not state.restart_triggered:  # Run until TuRBO converges
#     # Fit a GP model
#     train_Y = (Y_turbo - Y_turbo.mean()) / Y_turbo.std()
#     likelihood = GaussianLikelihood()
#     covar_module = ScaleKernel(  # Use the same lengthscale prior as in the TuRBO paper
#         MaternKernel(
#             nu=2.5, ard_num_dims=dim#, lengthscale_constraint=Interval(0.005, 4.0)
#         )
#     )
#     model = SingleTaskGP(
#         X_turbo, train_Y, covar_module=covar_module, likelihood=likelihood
#     )
#     mll = ExactMarginalLogLikelihood(model.likelihood, model)

#     # Do the fitting and acquisition function optimization inside the Cholesky context
#     with gpytorch.settings.max_cholesky_size(max_cholesky_size):
#         # Fit the model
#         fit_gpytorch_mll(mll)

#         # Create a batch
#         X_next = generate_batch(
#             state=state,
#             model=model,
#             X=X_turbo,
#             Y=train_Y,
#             batch_size=batch_size,
#             n_candidates=N_CANDIDATES,
#             num_restarts=NUM_RESTARTS,
#             raw_samples=RAW_SAMPLES,
#             acqf="ts",
#         )

#     Y_next = torch.tensor(
#         [eval_objective(x) for x in X_next], dtype=dtype, device=device
#     ).unsqueeze(-1)

#     # Update state
#     state = update_state(state=state, Y_next=Y_next)

#     # Append data
#     X_turbo = torch.cat((X_turbo, X_next), dim=0)
#     Y_turbo = torch.cat((Y_turbo, Y_next), dim=0)

#     # Print current status
#     print(
#         f"{len(X_turbo)}) Best value: {state.best_value:.2e}, TR length: {state.length:.2e}"
#     )
