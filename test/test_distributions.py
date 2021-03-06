"""
Note [Randomized statistical tests]
-----------------------------------

This note describes how to maintain tests in this file as random sources
change. This file contains two types of randomized tests:

1. The easier type of randomized test are tests that should always pass but are
   initialized with random data. If these fail something is wrong, but it's
   fine to use a fixed seed by inheriting from common.TestCase.

2. The trickier tests are statistical tests. These tests explicitly call
   set_rng_seed(n) and are marked "see Note [Randomized statistical tests]".
   These statistical tests have a known positive failure rate
   (we set failure_rate=1e-3 by default). We need to balance strength of these
   tests with annoyance of false alarms. One way that works is to specifically
   set seeds in each of the randomized tests. When a random generator
   occasionally changes (as in #4312 vectorizing the Box-Muller sampler), some
   of these statistical tests may (rarely) fail. If one fails in this case,
   it's fine to increment the seed of the failing test (but you shouldn't need
   to increment it more than once; otherwise something is probably actually
   wrong).
"""

import math
import numbers
import unittest
from collections import namedtuple
from itertools import product
from random import shuffle

import torch
from common import TestCase, run_tests, set_rng_seed
from torch.autograd import Variable, grad, gradcheck, variable
from torch.distributions import (Bernoulli, Beta, Binomial, Categorical,
                                 Cauchy, Chi2, Dirichlet, Distribution,
                                 Exponential, ExponentialFamily,
                                 FisherSnedecor, Gamma, Geometric,
                                 Gumbel, Laplace, LogNormal, Multinomial,
                                 Normal, OneHotCategorical, Pareto, Poisson,
                                 RelaxedBernoulli, RelaxedOneHotCategorical, StudentT,
                                 TransformedDistribution, Uniform, constraints,
                                 kl_divergence)
from torch.distributions.kl import _kl_expfamily_expfamily
from torch.distributions.constraint_registry import biject_to, transform_to
from torch.distributions.constraints import Constraint, is_dependent
from torch.distributions.dirichlet import _Dirichlet_backward
from torch.distributions.transforms import (AbsTransform, AffineTransform,
                                            BoltzmannTransform,
                                            ComposeTransform, ExpTransform,
                                            LowerCholeskyTransform,
                                            SigmoidTransform,
                                            StickBreakingTransform,
                                            identity_transform)
from torch.distributions.utils import _finfo, probs_to_logits, softmax

TEST_NUMPY = True
try:
    import numpy as np
    import scipy.stats
    import scipy.special
except ImportError:
    TEST_NUMPY = False

SCALAR_SHAPE = () if torch._C._with_scalars() else (1,)
TEST_CUDA = torch.cuda.is_available()


def pairwise(Dist, *params):
    """
    Creates a pair of distributions `Dist` initialzed to test each element of
    param with each other.
    """
    params1 = [variable([p] * len(p)) for p in params]
    params2 = [p.transpose(0, 1) for p in params1]
    return Dist(*params1), Dist(*params2)


def is_all_nan(tensor):
    """
    Checks if all entries of a tensor is nan.
    """
    return (tensor != tensor).all()


# Register all distributions for generic tests.
Example = namedtuple('Example', ['Dist', 'params'])
EXAMPLES = [
    Example(Bernoulli, [
        {'probs': Variable(torch.Tensor([0.7, 0.2, 0.4]), requires_grad=True)},
        {'probs': Variable(torch.Tensor([0.3]), requires_grad=True)},
        {'probs': 0.3},
    ]),
    Example(Geometric, [
        {'probs': Variable(torch.Tensor([0.7, 0.2, 0.4]), requires_grad=True)},
        {'probs': Variable(torch.Tensor([0.3]), requires_grad=True)},
        {'probs': 0.3},
    ]),
    Example(Beta, [
        {
            'concentration1': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True),
            'concentration0': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True),
        },
        {
            'concentration1': Variable(torch.exp(torch.randn(4)), requires_grad=True),
            'concentration0': Variable(torch.exp(torch.randn(4)), requires_grad=True),
        },
    ]),
    Example(Categorical, [
        {'probs': Variable(torch.Tensor([[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]), requires_grad=True)},
        {'probs': Variable(torch.Tensor([[1.0, 0.0], [0.0, 1.0]]), requires_grad=True)},
    ]),
    Example(Binomial, [
        {'probs': Variable(torch.Tensor([[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]), requires_grad=True), 'total_count': 10},
        {'probs': Variable(torch.Tensor([[1.0, 0.0], [0.0, 1.0]]), requires_grad=True), 'total_count': 10},
    ]),
    Example(Multinomial, [
        {'probs': Variable(torch.Tensor([[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]), requires_grad=True), 'total_count': 10},
        {'probs': Variable(torch.Tensor([[1.0, 0.0], [0.0, 1.0]]), requires_grad=True), 'total_count': 10},
    ]),
    Example(Cauchy, [
        {'loc': 0.0, 'scale': 1.0},
        {'loc': Variable(torch.Tensor([0.0])), 'scale': 1.0},
        {'loc': Variable(torch.Tensor([[0.0], [0.0]])),
         'scale': Variable(torch.Tensor([[1.0], [1.0]]))}
    ]),
    Example(Chi2, [
        {'df': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)},
        {'df': Variable(torch.exp(torch.randn(1)), requires_grad=True)},
    ]),
    Example(StudentT, [
        {'df': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)},
        {'df': Variable(torch.exp(torch.randn(1)), requires_grad=True)},
    ]),
    Example(Dirichlet, [
        {'concentration': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)},
        {'concentration': Variable(torch.exp(torch.randn(4)), requires_grad=True)},
    ]),
    Example(Exponential, [
        {'rate': Variable(torch.randn(5, 5).abs(), requires_grad=True)},
        {'rate': Variable(torch.randn(1).abs(), requires_grad=True)},
    ]),
    Example(FisherSnedecor, [
        {
            'df1': Variable(torch.randn(5, 5).abs(), requires_grad=True),
            'df2': Variable(torch.randn(5, 5).abs(), requires_grad=True),
        },
        {
            'df1': Variable(torch.randn(1).abs(), requires_grad=True),
            'df2': Variable(torch.randn(1).abs(), requires_grad=True),
        },
        {
            'df1': Variable(torch.Tensor([1.0])),
            'df2': 1.0,
        }
    ]),
    Example(Gamma, [
        {
            'concentration': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True),
            'rate': Variable(torch.exp(torch.randn(2, 3)), requires_grad=True),
        },
        {
            'concentration': Variable(torch.exp(torch.randn(1)), requires_grad=True),
            'rate': Variable(torch.exp(torch.randn(1)), requires_grad=True),
        },
    ]),
    Example(Gumbel, [
        {
            'loc': Variable(torch.randn(5, 5), requires_grad=True),
            'scale': Variable(torch.randn(5, 5).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.randn(1), requires_grad=True),
            'scale': Variable(torch.randn(1).abs(), requires_grad=True),
        },
    ]),
    Example(Laplace, [
        {
            'loc': Variable(torch.randn(5, 5), requires_grad=True),
            'scale': Variable(torch.randn(5, 5).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.randn(1), requires_grad=True),
            'scale': Variable(torch.randn(1).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.Tensor([1.0, 0.0]), requires_grad=True),
            'scale': Variable(torch.Tensor([1e-5, 1e-5]), requires_grad=True),
        },
    ]),
    Example(LogNormal, [
        {
            'loc': Variable(torch.randn(5, 5), requires_grad=True),
            'scale': Variable(torch.randn(5, 5).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.randn(1), requires_grad=True),
            'scale': Variable(torch.randn(1).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.Tensor([1.0, 0.0]), requires_grad=True),
            'scale': Variable(torch.Tensor([1e-5, 1e-5]), requires_grad=True),
        },
    ]),
    Example(Normal, [
        {
            'loc': Variable(torch.randn(5, 5), requires_grad=True),
            'scale': Variable(torch.randn(5, 5).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.randn(1), requires_grad=True),
            'scale': Variable(torch.randn(1).abs(), requires_grad=True),
        },
        {
            'loc': Variable(torch.Tensor([1.0, 0.0]), requires_grad=True),
            'scale': Variable(torch.Tensor([1e-5, 1e-5]), requires_grad=True),
        },
    ]),
    Example(OneHotCategorical, [
        {'probs': Variable(torch.Tensor([[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]), requires_grad=True)},
        {'probs': Variable(torch.Tensor([[1.0, 0.0], [0.0, 1.0]]), requires_grad=True)},
    ]),
    Example(Pareto, [
        {
            'scale': 1.0,
            'alpha': 1.0
        },
        {
            'scale': Variable(torch.randn(5, 5).abs(), requires_grad=True),
            'alpha': Variable(torch.randn(5, 5).abs(), requires_grad=True)
        },
        {
            'scale': variable([1.0]),
            'alpha': 1.0
        }
    ]),
    Example(Poisson, [
        {
            'rate': Variable(torch.randn(5, 5).abs(), requires_grad=True),
        },
        {
            'rate': Variable(torch.randn(3).abs(), requires_grad=True),
        },
        {
            'rate': 0.2,
        }
    ]),
    Example(RelaxedBernoulli, [
        {
            'temperature': Variable(torch.Tensor([0.5]), requires_grad=True),
            'probs': Variable(torch.Tensor([0.7, 0.2, 0.4]), requires_grad=True),
        },
        {
            'temperature': Variable(torch.Tensor([2.0])),
            'probs': Variable(torch.Tensor([0.3])),
        },
        {
            'temperature': Variable(torch.Tensor([7.2])),
            'logits': Variable(torch.Tensor([-2.0, 2.0, 1.0, 5.0]))
        }
    ]),
    Example(RelaxedOneHotCategorical, [
        {
            'temperature': Variable(torch.Tensor([0.5]), requires_grad=True),
            'probs': Variable(torch.Tensor([[0.1, 0.2, 0.7], [0.5, 0.3, 0.2]]), requires_grad=True)
        },
        {
            'temperature': Variable(torch.Tensor([2.0])),
            'probs': Variable(torch.Tensor([[1.0, 0.0], [0.0, 1.0]]))
        },
        {
            'temperature': Variable(torch.Tensor([7.2])),
            'logits': Variable(torch.Tensor([[-2.0, 2.0], [1.0, 5.0]]))
        }
    ]),
    Example(TransformedDistribution, [
        {
            'base_distribution': Normal(Variable(torch.randn(2, 3), requires_grad=True),
                                        Variable(torch.randn(2, 3).abs(), requires_grad=True)),
            'transforms': [],
        },
        {
            'base_distribution': Normal(Variable(torch.randn(2, 3), requires_grad=True),
                                        Variable(torch.randn(2, 3).abs(), requires_grad=True)),
            'transforms': ExpTransform(),
        },
        {
            'base_distribution': Normal(Variable(torch.randn(2, 3, 5), requires_grad=True),
                                        Variable(torch.randn(2, 3, 5).abs(), requires_grad=True)),
            'transforms': [AffineTransform(Variable(torch.randn(3, 5)), Variable(torch.randn(3, 5))),
                           ExpTransform()],
        },
    ]),
    Example(Uniform, [
        {
            'low': Variable(torch.zeros(5, 5), requires_grad=True),
            'high': Variable(torch.ones(5, 5), requires_grad=True),
        },
        {
            'low': Variable(torch.zeros(1), requires_grad=True),
            'high': Variable(torch.ones(1), requires_grad=True),
        },
        {
            'low': Variable(torch.Tensor([1.0, 1.0]), requires_grad=True),
            'high': Variable(torch.Tensor([2.0, 3.0]), requires_grad=True),
        },
    ]),
]


def unwrap(value):
    if isinstance(value, Variable):
        return value.data
    return value


class TestDistributions(TestCase):
    def _gradcheck_log_prob(self, dist_ctor, ctor_params):
        # performs gradient checks on log_prob
        distribution = dist_ctor(*ctor_params)
        s = distribution.sample()

        expected_shape = distribution.batch_shape + distribution.event_shape
        if not expected_shape and not torch._C._with_scalars():
            expected_shape = torch.Size((1,))  # Work around lack of scalars.
        self.assertEqual(s.size(), expected_shape)

        def apply_fn(*params):
            return dist_ctor(*params).log_prob(s)

        gradcheck(apply_fn, ctor_params, raise_exception=True)

    def _check_log_prob(self, dist, asset_fn):
        # checks that the log_prob matches a reference function
        s = dist.sample()
        log_probs = dist.log_prob(s)
        for i, (val, log_prob) in enumerate(zip(s.data.view(-1), log_probs.data.view(-1))):
            asset_fn(i, val, log_prob)

    def _check_sampler_sampler(self, torch_dist, ref_dist, message, multivariate=False,
                               num_samples=10000, failure_rate=1e-3):
        # Checks that the .sample() method matches a reference function.
        torch_samples = torch_dist.sample((num_samples,)).squeeze()
        if isinstance(torch_samples, Variable):
            torch_samples = torch_samples.data
        torch_samples = torch_samples.cpu().numpy()
        ref_samples = ref_dist.rvs(num_samples).astype(np.float64)
        if multivariate:
            # Project onto a random axis.
            axis = np.random.normal(size=torch_samples.shape[-1])
            axis /= np.linalg.norm(axis)
            torch_samples = np.dot(torch_samples, axis)
            ref_samples = np.dot(ref_samples, axis)
        samples = [(x, +1) for x in torch_samples] + [(x, -1) for x in ref_samples]
        shuffle(samples)  # necessary to prevent stable sort from making uneven bins for discrete
        samples.sort(key=lambda x: x[0])
        samples = np.array(samples)[:, 1]

        # Aggragate into bins filled with roughly zero-mean unit-variance RVs.
        num_bins = 10
        samples_per_bin = len(samples) // num_bins
        bins = samples.reshape((num_bins, samples_per_bin)).mean(axis=1)
        stddev = samples_per_bin ** -0.5
        threshold = stddev * scipy.special.erfinv(1 - 2 * failure_rate / num_bins)
        message = '{}.sample() is biased:\n{}'.format(message, bins)
        for bias in bins:
            self.assertLess(-threshold, bias, message)
            self.assertLess(bias, threshold, message)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def _check_sampler_discrete(self, torch_dist, ref_dist, message,
                                num_samples=10000, failure_rate=1e-3):
        """Runs a Chi2-test for the support, but ignores tail instead of combining"""
        torch_samples = torch_dist.sample((num_samples,)).squeeze()
        if isinstance(torch_samples, Variable):
            torch_samples = torch_samples.data
        torch_samples = torch_samples.cpu().numpy()
        unique, counts = np.unique(torch_samples, return_counts=True)
        pmf = ref_dist.pmf(unique)
        msk = (counts > 5) & ((pmf * num_samples) > 5)
        self.assertGreater(pmf[msk].sum(), 0.9, "Distribution is too sparse for test; try increasing num_samples")
        chisq, p = scipy.stats.chisquare(counts[msk], pmf[msk] * num_samples)
        self.assertGreater(p, failure_rate, message)

    def _check_enumerate_support(self, dist, examples):
        for param, expected in examples:
            param = torch.Tensor(param)
            expected = torch.Tensor(expected)
            actual = dist(param).enumerate_support()
            self.assertEqual(actual, expected)
            param = Variable(param)
            expected = Variable(expected)
            actual = dist(param).enumerate_support()
            self.assertEqual(actual, expected)

    def test_enumerate_support_type(self):
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                try:
                    self.assertTrue(type(unwrap(dist.sample())) is type(unwrap(dist.enumerate_support())),
                                    msg=('{} example {}/{}, return type mismatch between ' +
                                         'sample and enumerate_support.').format(Dist.__name__, i + 1, len(params)))
                except NotImplementedError:
                    pass

    def test_has_examples(self):
        distributions_with_examples = set(e.Dist for e in EXAMPLES)
        for Dist in globals().values():
            if isinstance(Dist, type) and issubclass(Dist, Distribution) \
                    and Dist is not Distribution and Dist is not ExponentialFamily:
                self.assertIn(Dist, distributions_with_examples,
                              "Please add {} to the EXAMPLES list in test_distributions.py".format(Dist.__name__))

    def test_bernoulli(self):
        p = variable([0.7, 0.2, 0.4], requires_grad=True)
        r = variable(0.3, requires_grad=True)
        s = 0.3
        self.assertEqual(Bernoulli(p).sample((8,)).size(), (8, 3))
        self.assertTrue(isinstance(Bernoulli(p).sample().data, torch.Tensor))
        self.assertEqual(Bernoulli(r).sample((8,)).size(), (8,) + SCALAR_SHAPE)
        self.assertEqual(Bernoulli(r).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Bernoulli(r).sample((3, 2)).size(), (3, 2,) + SCALAR_SHAPE)
        self.assertEqual(Bernoulli(s).sample().size(), SCALAR_SHAPE)
        self._gradcheck_log_prob(Bernoulli, (p,))

        def ref_log_prob(idx, val, log_prob):
            prob = p.data[idx]
            self.assertEqual(log_prob, math.log(prob if val else 1 - prob))

        self._check_log_prob(Bernoulli(p), ref_log_prob)
        self._check_log_prob(Bernoulli(logits=p.log() - (-p).log1p()), ref_log_prob)
        self.assertRaises(NotImplementedError, Bernoulli(r).rsample)

        # check entropy computation
        self.assertEqual(Bernoulli(p).entropy().data, torch.Tensor([0.6108, 0.5004, 0.6730]), prec=1e-4)
        self.assertEqual(Bernoulli(torch.Tensor([0.0])).entropy(), torch.Tensor([0.0]))
        self.assertEqual(Bernoulli(s).entropy(), torch.Tensor([0.6108]), prec=1e-4)

    def test_bernoulli_enumerate_support(self):
        examples = [
            ([0.1], [[0], [1]]),
            ([0.1, 0.9], [[0, 0], [1, 1]]),
            ([[0.1, 0.2], [0.3, 0.4]], [[[0, 0], [0, 0]], [[1, 1], [1, 1]]]),
        ]
        self._check_enumerate_support(Bernoulli, examples)

    def test_bernoulli_3d(self):
        p = Variable(torch.Tensor(2, 3, 5).fill_(0.5), requires_grad=True)
        self.assertEqual(Bernoulli(p).sample().size(), (2, 3, 5))
        self.assertEqual(Bernoulli(p).sample(sample_shape=(2, 5)).size(),
                         (2, 5, 2, 3, 5))
        self.assertEqual(Bernoulli(p).sample((2,)).size(), (2, 2, 3, 5))

    def test_geometric(self):
        p = variable([0.7, 0.2, 0.4], requires_grad=True)
        r = variable(0.3, requires_grad=True)
        s = 0.3
        self.assertEqual(Geometric(p).sample((8,)).size(), (8, 3))
        self.assertEqual(Geometric(1).sample(), 0)
        self.assertEqual(Geometric(1).log_prob(variable(1)), -float('inf'), allow_inf=True)
        self.assertEqual(Geometric(1).log_prob(variable(0)), 0)
        self.assertTrue(isinstance(Geometric(p).sample().data, torch.Tensor))
        self.assertEqual(Geometric(r).sample((8,)).size(), (8,) + SCALAR_SHAPE)
        self.assertEqual(Geometric(r).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Geometric(r).sample((3, 2)).size(), (3, 2) + SCALAR_SHAPE)
        self.assertEqual(Geometric(s).sample().size(), SCALAR_SHAPE)
        self._gradcheck_log_prob(Geometric, (p,))
        self.assertRaises(ValueError, lambda: Geometric(0))
        self.assertRaises(NotImplementedError, Geometric(r).rsample)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_geometric_log_prob_and_entropy(self):
        p = Variable(torch.Tensor([0.7, 0.2, 0.4]), requires_grad=True)
        s = 0.3

        def ref_log_prob(idx, val, log_prob):
            prob = p.data[idx]
            self.assertEqual(log_prob, scipy.stats.geom(prob, loc=-1).logpmf(val))

        self._check_log_prob(Geometric(p), ref_log_prob)
        self._check_log_prob(Geometric(logits=p.log() - (-p).log1p()), ref_log_prob)

        # check entropy computation
        self.assertEqual(Geometric(p).entropy().data, scipy.stats.geom(p.data.numpy(), loc=-1).entropy(), prec=1e-3)
        self.assertEqual(float(Geometric(s).entropy()), scipy.stats.geom(s, loc=-1).entropy().item(), prec=1e-3)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_geometric_sample(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for prob in [0.01, 0.18, 0.8]:
            self._check_sampler_discrete(Geometric(prob),
                                         scipy.stats.geom(p=prob, loc=-1),
                                         'Geometric(prob={})'.format(prob))

    def test_binomial(self):
        p = Variable(torch.arange(0.05, 1, 0.1), requires_grad=True)
        for total_count in [1, 2, 10]:
            self._gradcheck_log_prob(lambda p: Binomial(total_count, p), [p])
            self._gradcheck_log_prob(lambda p: Binomial(total_count, None, p.log()), [p])
        self.assertRaises(NotImplementedError, Binomial(10, p).rsample)
        self.assertRaises(NotImplementedError, Binomial(10, p).entropy)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_binomial_log_prob(self):
        probs = Variable(torch.arange(0.05, 1, 0.1))
        for total_count in [1, 2, 10]:

            def ref_log_prob(idx, x, log_prob):
                p = probs.data.view(-1)[idx]
                expected = scipy.stats.binom(total_count, p).logpmf(x)
                self.assertAlmostEqual(log_prob, expected, places=3)

            self._check_log_prob(Binomial(total_count, probs), ref_log_prob)
            logits = probs_to_logits(probs, is_binary=True)
            self._check_log_prob(Binomial(total_count, logits=logits), ref_log_prob)

    def test_binomial_extreme_vals(self):
        total_count = 100
        bin0 = Binomial(total_count, 0)
        self.assertEqual(bin0.sample(), 0)
        self.assertAlmostEqual(bin0.log_prob(variable([0]))[0], 0, places=3)
        self.assertEqual(float(bin0.log_prob(variable([1])).exp()), 0, allow_inf=True)
        bin1 = Binomial(total_count, 1)
        self.assertEqual(bin1.sample(), total_count)
        self.assertAlmostEqual(bin1.log_prob(variable([total_count]))[0], 0, places=3)
        self.assertEqual(float(bin1.log_prob(variable([total_count - 1])).exp()), 0, allow_inf=True)

    def test_multinomial_1d(self):
        total_count = 10
        p = Variable(torch.Tensor([0.1, 0.2, 0.3]), requires_grad=True)
        self.assertEqual(Multinomial(total_count, p).sample().size(), (3,))
        self.assertEqual(Multinomial(total_count, p).sample((2, 2)).size(), (2, 2, 3))
        self.assertEqual(Multinomial(total_count, p).sample((1,)).size(), (1, 3))
        self._gradcheck_log_prob(lambda p: Multinomial(total_count, p), [p])
        self._gradcheck_log_prob(lambda p: Multinomial(total_count, None, p.log()), [p])
        self.assertRaises(NotImplementedError, Multinomial(10, p).rsample)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_multinomial_1d_log_prob(self):
        total_count = 10
        p = Variable(torch.Tensor([0.1, 0.2, 0.3]), requires_grad=True)
        dist = Multinomial(total_count, probs=p)
        x = dist.sample()
        log_prob = dist.log_prob(x)
        expected = torch.Tensor(scipy.stats.multinomial.logpmf(x.numpy(), n=total_count, p=dist.probs.detach().numpy()))
        self.assertEqual(log_prob.data, expected)

        dist = Multinomial(total_count, logits=p.log())
        x = dist.sample()
        log_prob = dist.log_prob(x)
        expected = torch.Tensor(scipy.stats.multinomial.logpmf(x.numpy(), n=total_count, p=dist.probs.detach().numpy()))
        self.assertEqual(log_prob.data, expected)

    def test_multinomial_2d(self):
        total_count = 10
        probabilities = [[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]
        probabilities_1 = [[1.0, 0.0], [0.0, 1.0]]
        p = Variable(torch.Tensor(probabilities), requires_grad=True)
        s = Variable(torch.Tensor(probabilities_1), requires_grad=True)
        self.assertEqual(Multinomial(total_count, p).sample().size(), (2, 3))
        self.assertEqual(Multinomial(total_count, p).sample(sample_shape=(3, 4)).size(), (3, 4, 2, 3))
        self.assertEqual(Multinomial(total_count, p).sample((6,)).size(), (6, 2, 3))
        set_rng_seed(0)
        self._gradcheck_log_prob(lambda p: Multinomial(total_count, p), [p])
        p.grad.zero_()
        self._gradcheck_log_prob(lambda p: Multinomial(total_count, None, p.log()), [p])

        # sample check for extreme value of probs
        self.assertEqual(Multinomial(total_count, s).sample().data,
                         torch.Tensor([[total_count, 0], [0, total_count]]))

        # check entropy computation
        self.assertRaises(NotImplementedError, Multinomial(10, p).entropy)

    def test_categorical_1d(self):
        p = Variable(torch.Tensor([0.1, 0.2, 0.3]), requires_grad=True)
        self.assertTrue(is_all_nan(Categorical(p).mean))
        self.assertTrue(is_all_nan(Categorical(p).variance))
        self.assertEqual(Categorical(p).sample().size(), SCALAR_SHAPE)
        self.assertTrue(isinstance(Categorical(p).sample().data, torch.LongTensor))
        self.assertEqual(Categorical(p).sample((2, 2)).size(), (2, 2))
        self.assertEqual(Categorical(p).sample((1,)).size(), (1,))
        self._gradcheck_log_prob(Categorical, (p,))
        self.assertRaises(NotImplementedError, Categorical(p).rsample)

    def test_categorical_2d(self):
        probabilities = [[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]
        probabilities_1 = [[1.0, 0.0], [0.0, 1.0]]
        p = Variable(torch.Tensor(probabilities), requires_grad=True)
        s = Variable(torch.Tensor(probabilities_1), requires_grad=True)
        self.assertEqual(Categorical(p).mean.size(), (2,))
        self.assertEqual(Categorical(p).variance.size(), (2,))
        self.assertTrue(is_all_nan(Categorical(p).mean))
        self.assertTrue(is_all_nan(Categorical(p).variance))
        self.assertEqual(Categorical(p).sample().size(), (2,))
        self.assertEqual(Categorical(p).sample(sample_shape=(3, 4)).size(), (3, 4, 2))
        self.assertEqual(Categorical(p).sample((6,)).size(), (6, 2))
        self._gradcheck_log_prob(Categorical, (p,))

        # sample check for extreme value of probs
        set_rng_seed(0)
        self.assertEqual(Categorical(s).sample(sample_shape=(2,)).data,
                         torch.Tensor([[0, 1], [0, 1]]))

        def ref_log_prob(idx, val, log_prob):
            sample_prob = p.data[idx][val] / p.data[idx].sum()
            self.assertEqual(log_prob, math.log(sample_prob))

        self._check_log_prob(Categorical(p), ref_log_prob)
        self._check_log_prob(Categorical(logits=p.log()), ref_log_prob)

        # check entropy computation
        self.assertEqual(Categorical(p).entropy().data, torch.Tensor([1.0114, 1.0297]), prec=1e-4)
        self.assertEqual(Categorical(s).entropy().data, torch.Tensor([0.0, 0.0]))

    def test_categorical_enumerate_support(self):
        examples = [
            ([0.1, 0.2, 0.7], [0, 1, 2]),
            ([[0.1, 0.9], [0.3, 0.7]], [[0, 0], [1, 1]]),
        ]
        self._check_enumerate_support(Categorical, examples)

    def test_one_hot_categorical_1d(self):
        p = Variable(torch.Tensor([0.1, 0.2, 0.3]), requires_grad=True)
        self.assertEqual(OneHotCategorical(p).sample().size(), (3,))
        self.assertTrue(isinstance(OneHotCategorical(p).sample().data, torch.Tensor))
        self.assertEqual(OneHotCategorical(p).sample((2, 2)).size(), (2, 2, 3))
        self.assertEqual(OneHotCategorical(p).sample((1,)).size(), (1, 3))
        self._gradcheck_log_prob(OneHotCategorical, (p,))
        self.assertRaises(NotImplementedError, OneHotCategorical(p).rsample)

    def test_one_hot_categorical_2d(self):
        probabilities = [[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]
        probabilities_1 = [[1.0, 0.0], [0.0, 1.0]]
        p = Variable(torch.Tensor(probabilities), requires_grad=True)
        s = Variable(torch.Tensor(probabilities_1), requires_grad=True)
        self.assertEqual(OneHotCategorical(p).sample().size(), (2, 3))
        self.assertEqual(OneHotCategorical(p).sample(sample_shape=(3, 4)).size(), (3, 4, 2, 3))
        self.assertEqual(OneHotCategorical(p).sample((6,)).size(), (6, 2, 3))
        self._gradcheck_log_prob(OneHotCategorical, (p,))

        dist = OneHotCategorical(p)
        x = dist.sample()
        self.assertEqual(dist.log_prob(x), Categorical(p).log_prob(x.max(-1)[1]))

    def test_one_hot_categorical_enumerate_support(self):
        examples = [
            ([0.1, 0.2, 0.7], [[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
            ([[0.1, 0.9], [0.3, 0.7]], [[[1, 0], [1, 0]], [[0, 1], [0, 1]]]),
        ]
        self._check_enumerate_support(OneHotCategorical, examples)

    def test_poisson_shape(self):
        rate = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        rate_1d = Variable(torch.randn(1).abs(), requires_grad=True)
        self.assertEqual(Poisson(rate).sample().size(), (2, 3))
        self.assertEqual(Poisson(rate).sample((7,)).size(), (7, 2, 3))
        self.assertEqual(Poisson(rate_1d).sample().size(), (1,))
        self.assertEqual(Poisson(rate_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Poisson(2.0).sample((2,)).size(), (2,))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_poisson_log_prob(self):
        rate = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        rate_1d = Variable(torch.randn(1).abs(), requires_grad=True)

        def ref_log_prob(idx, x, log_prob):
            l = rate.data.view(-1)[idx]
            expected = scipy.stats.poisson.logpmf(x, l)
            self.assertAlmostEqual(log_prob, expected, places=3)

        set_rng_seed(0)
        self._check_log_prob(Poisson(rate), ref_log_prob)
        self._gradcheck_log_prob(Poisson, (rate,))
        self._gradcheck_log_prob(Poisson, (rate_1d,))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_poisson_sample(self):
        set_rng_seed(1)  # see Note [Randomized statistical tests]
        for rate in [0.1, 1.0, 5.0]:
            self._check_sampler_discrete(Poisson(rate),
                                         scipy.stats.poisson(rate),
                                         'Poisson(lambda={})'.format(rate),
                                         failure_rate=1e-3)

    @unittest.skipIf(not TEST_CUDA, "CUDA not found")
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_poisson_gpu_sample(self):
        set_rng_seed(0)
        for rate in [0.12, 0.9, 4.0]:
            self._check_sampler_discrete(Poisson(torch.Tensor([rate]).cuda()),
                                         scipy.stats.poisson(rate),
                                         'Poisson(lambda={}, cuda)'.format(rate),
                                         failure_rate=1e-3)

    def test_relaxed_bernoulli(self):
        p = variable([0.7, 0.2, 0.4], requires_grad=True)
        r = variable(0.3, requires_grad=True)
        s = 0.3
        temp = variable(0.67, requires_grad=True)
        self.assertEqual(RelaxedBernoulli(temp, p).sample((8,)).size(), (8, 3))
        self.assertTrue(isinstance(RelaxedBernoulli(temp, p).sample().data, torch.Tensor))
        self.assertEqual(RelaxedBernoulli(temp, r).sample((8,)).size(), (8,) + SCALAR_SHAPE)
        self.assertEqual(RelaxedBernoulli(temp, r).sample().size(), SCALAR_SHAPE)
        self.assertEqual(RelaxedBernoulli(temp, r).sample((3, 2)).size(), (3, 2,) + SCALAR_SHAPE)
        self.assertEqual(RelaxedBernoulli(temp, s).sample().size(), SCALAR_SHAPE)
        self._gradcheck_log_prob(RelaxedBernoulli, (temp, p))
        self._gradcheck_log_prob(RelaxedBernoulli, (temp, r))

        # test that rsample doesn't fail
        s = RelaxedBernoulli(temp, p).rsample()
        s.backward(torch.ones_like(s))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_rounded_relaxed_bernoulli(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]

        class Rounded(object):
            def __init__(self, dist):
                self.dist = dist

            def sample(self, *args, **kwargs):
                return torch.round(self.dist.sample(*args, **kwargs))

        for probs, temp in product([0.1, 0.2, 0.8], [0.1, 1.0, 10.0]):
            self._check_sampler_discrete(Rounded(RelaxedBernoulli(temp, probs)),
                                         scipy.stats.bernoulli(probs),
                                         'Rounded(RelaxedBernoulli(temp={}, probs={}))'.format(temp, probs),
                                         failure_rate=1e-3)

        for probs in [0.001, 0.2, 0.999]:
            equal_probs = torch.Tensor([0.5])
            dist = RelaxedBernoulli(1e10, probs)
            s = dist.rsample()
            self.assertEqual(equal_probs, s)

    def test_relaxed_one_hot_categorical_1d(self):
        p = Variable(torch.Tensor([0.1, 0.2, 0.3]), requires_grad=True)
        temp = variable(0.67, requires_grad=True)
        self.assertEqual(RelaxedOneHotCategorical(probs=p, temperature=temp).sample().size(), (3,))
        self.assertTrue(isinstance(RelaxedOneHotCategorical(probs=p, temperature=temp).sample().data, torch.Tensor))
        self.assertEqual(RelaxedOneHotCategorical(probs=p, temperature=temp).sample((2, 2)).size(), (2, 2, 3))
        self.assertEqual(RelaxedOneHotCategorical(probs=p, temperature=temp).sample_n(1).size(), (1, 3))
        self._gradcheck_log_prob(RelaxedOneHotCategorical, (temp, p))

    def test_relaxed_one_hot_categorical_2d(self):
        probabilities = [[0.1, 0.2, 0.3], [0.5, 0.3, 0.2]]
        probabilities_1 = [[1.0, 0.0], [0.0, 1.0]]
        temp = Variable(torch.Tensor([3.00]), requires_grad=True)
        temp_2 = Variable(torch.Tensor([0.2]), requires_grad=True)
        p = Variable(torch.Tensor(probabilities), requires_grad=True)
        s = Variable(torch.Tensor(probabilities_1), requires_grad=True)
        self.assertEqual(RelaxedOneHotCategorical(temp, p).sample().size(), (2, 3))
        self.assertEqual(RelaxedOneHotCategorical(temp, p).sample(sample_shape=(3, 4)).size(), (3, 4, 2, 3))
        self.assertEqual(RelaxedOneHotCategorical(temp, p).sample_n(6).size(), (6, 2, 3))
        self._gradcheck_log_prob(RelaxedOneHotCategorical, (temp, p))
        self._gradcheck_log_prob(RelaxedOneHotCategorical, (temp_2, p))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_argmax_relaxed_categorical(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]

        class ArgMax(object):
            def __init__(self, dist):
                self.dist = dist

            def sample(self, *args, **kwargs):
                s = self.dist.sample(*args, **kwargs)
                _, idx = torch.max(s, -1)
                return idx

        class ScipyCategorical(object):
            def __init__(self, dist):
                self.dist = dist

            def pmf(self, samples):
                new_samples = np.zeros(samples.shape + self.dist.p.shape)
                new_samples[np.arange(samples.shape[0]), samples] = 1
                return self.dist.pmf(new_samples)

        for probs, temp in product([torch.Tensor([0.1, 0.9]), torch.Tensor([0.2, 0.2, 0.6])], [0.1, 1.0, 10.0]):
            self._check_sampler_discrete(ArgMax(RelaxedOneHotCategorical(temp, probs)),
                                         ScipyCategorical(scipy.stats.multinomial(1, probs)),
                                         'Rounded(RelaxedOneHotCategorical(temp={}, probs={}))'.format(temp, probs),
                                         failure_rate=1e-3)

        for probs in [torch.Tensor([0.1, 0.9]), torch.Tensor([0.2, 0.2, 0.6])]:
            equal_probs = torch.ones(probs.size()) / probs.size()[0]
            dist = RelaxedOneHotCategorical(1e10, probs)
            s = dist.rsample()
            self.assertEqual(equal_probs, s)

    def test_uniform(self):
        low = Variable(torch.zeros(5, 5), requires_grad=True)
        high = Variable(torch.ones(5, 5) * 3, requires_grad=True)
        low_1d = Variable(torch.zeros(1), requires_grad=True)
        high_1d = Variable(torch.ones(1) * 3, requires_grad=True)
        self.assertEqual(Uniform(low, high).sample().size(), (5, 5))
        self.assertEqual(Uniform(low, high).sample((7,)).size(), (7, 5, 5))
        self.assertEqual(Uniform(low_1d, high_1d).sample().size(), (1,))
        self.assertEqual(Uniform(low_1d, high_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Uniform(0.0, 1.0).sample((1,)).size(), (1,))

        # Check log_prob computation when value outside range
        uniform = Uniform(low_1d, high_1d)
        above_high = Variable(torch.Tensor([4.0]))
        below_low = Variable(torch.Tensor([-1.0]))
        self.assertEqual(uniform.log_prob(above_high).data[0], -float('inf'), allow_inf=True)
        self.assertEqual(uniform.log_prob(below_low).data[0], -float('inf'), allow_inf=True)

        set_rng_seed(1)
        self._gradcheck_log_prob(Uniform, (low, high))
        self._gradcheck_log_prob(Uniform, (low, 1.0))
        self._gradcheck_log_prob(Uniform, (0.0, high))

        state = torch.get_rng_state()
        rand = low.new(low.size()).uniform_()
        torch.set_rng_state(state)
        u = Uniform(low, high).rsample()
        u.backward(torch.ones_like(u))
        self.assertEqual(low.grad, 1 - rand)
        self.assertEqual(high.grad, rand)
        low.grad.zero_()
        high.grad.zero_()

    def test_cauchy(self):
        loc = Variable(torch.zeros(5, 5), requires_grad=True)
        scale = Variable(torch.ones(5, 5), requires_grad=True)
        loc_1d = Variable(torch.zeros(1), requires_grad=True)
        scale_1d = Variable(torch.ones(1), requires_grad=True)
        self.assertTrue(is_all_nan(Cauchy(loc_1d, scale_1d).mean))
        self.assertEqual(Cauchy(loc_1d, scale_1d).variance, float('inf'), allow_inf=True)
        self.assertEqual(Cauchy(loc, scale).sample().size(), (5, 5))
        self.assertEqual(Cauchy(loc, scale).sample((7,)).size(), (7, 5, 5))
        self.assertEqual(Cauchy(loc_1d, scale_1d).sample().size(), (1,))
        self.assertEqual(Cauchy(loc_1d, scale_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Cauchy(0.0, 1.0).sample((1,)).size(), (1,))

        set_rng_seed(1)
        self._gradcheck_log_prob(Uniform, (loc, scale))
        self._gradcheck_log_prob(Uniform, (loc, 1.0))
        self._gradcheck_log_prob(Uniform, (0.0, scale))

        state = torch.get_rng_state()
        eps = loc.new(loc.size()).cauchy_()
        torch.set_rng_state(state)
        c = Cauchy(loc, scale).rsample()
        c.backward(torch.ones_like(c))
        self.assertEqual(loc.grad, torch.ones_like(scale))
        self.assertEqual(scale.grad, eps)
        loc.grad.zero_()
        scale.grad.zero_()

    def test_lognormal(self):
        mean = Variable(torch.randn(5, 5), requires_grad=True)
        std = Variable(torch.randn(5, 5).abs(), requires_grad=True)
        mean_1d = Variable(torch.randn(1), requires_grad=True)
        std_1d = Variable(torch.randn(1), requires_grad=True)
        mean_delta = torch.Tensor([1.0, 0.0])
        std_delta = torch.Tensor([1e-5, 1e-5])
        self.assertEqual(LogNormal(mean, std).sample().size(), (5, 5))
        self.assertEqual(LogNormal(mean, std).sample((7,)).size(), (7, 5, 5))
        self.assertEqual(LogNormal(mean_1d, std_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(LogNormal(mean_1d, std_1d).sample().size(), (1,))
        self.assertEqual(LogNormal(0.2, .6).sample((1,)).size(), (1,))
        self.assertEqual(LogNormal(-0.7, 50.0).sample((1,)).size(), (1,))

        # sample check for extreme value of mean, std
        set_rng_seed(1)
        self.assertEqual(LogNormal(mean_delta, std_delta).sample(sample_shape=(1, 2)),
                         torch.Tensor([[[math.exp(1), 1.0], [math.exp(1), 1.0]]]),
                         prec=1e-4)

        self._gradcheck_log_prob(LogNormal, (mean, std))
        self._gradcheck_log_prob(LogNormal, (mean, 1.0))
        self._gradcheck_log_prob(LogNormal, (0.0, std))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_lognormal_logprob(self):
        mean = Variable(torch.randn(5, 1), requires_grad=True)
        std = Variable(torch.randn(5, 1).abs(), requires_grad=True)

        def ref_log_prob(idx, x, log_prob):
            m = mean.data.view(-1)[idx]
            s = std.data.view(-1)[idx]
            expected = scipy.stats.lognorm(s=s, scale=math.exp(m)).logpdf(x)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(LogNormal(mean, std), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_lognormal_sample(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for mean, std in product([-1.0, 0.0, 1.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(LogNormal(mean, std),
                                        scipy.stats.lognorm(scale=math.exp(mean), s=std),
                                        'LogNormal(loc={}, scale={})'.format(mean, std))

    def test_normal(self):
        loc = Variable(torch.randn(5, 5), requires_grad=True)
        scale = Variable(torch.randn(5, 5).abs(), requires_grad=True)
        loc_1d = Variable(torch.randn(1), requires_grad=True)
        scale_1d = Variable(torch.randn(1), requires_grad=True)
        loc_delta = torch.Tensor([1.0, 0.0])
        scale_delta = torch.Tensor([1e-5, 1e-5])
        self.assertEqual(Normal(loc, scale).sample().size(), (5, 5))
        self.assertEqual(Normal(loc, scale).sample((7,)).size(), (7, 5, 5))
        self.assertEqual(Normal(loc_1d, scale_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Normal(loc_1d, scale_1d).sample().size(), (1,))
        self.assertEqual(Normal(0.2, .6).sample((1,)).size(), (1,))
        self.assertEqual(Normal(-0.7, 50.0).sample((1,)).size(), (1,))

        # sample check for extreme value of mean, std
        set_rng_seed(1)
        self.assertEqual(Normal(loc_delta, scale_delta).sample(sample_shape=(1, 2)),
                         torch.Tensor([[[1.0, 0.0], [1.0, 0.0]]]),
                         prec=1e-4)

        self._gradcheck_log_prob(Normal, (loc, scale))
        self._gradcheck_log_prob(Normal, (loc, 1.0))
        self._gradcheck_log_prob(Normal, (0.0, scale))

        state = torch.get_rng_state()
        eps = torch.normal(torch.zeros_like(loc), torch.ones_like(scale))
        torch.set_rng_state(state)
        z = Normal(loc, scale).rsample()
        z.backward(torch.ones_like(z))
        self.assertEqual(loc.grad, torch.ones_like(loc))
        self.assertEqual(scale.grad, eps)
        loc.grad.zero_()
        scale.grad.zero_()
        self.assertEqual(z.size(), (5, 5))

        def ref_log_prob(idx, x, log_prob):
            m = loc.data.view(-1)[idx]
            s = scale.data.view(-1)[idx]
            expected = (math.exp(-(x - m) ** 2 / (2 * s ** 2)) /
                        math.sqrt(2 * math.pi * s ** 2))
            self.assertAlmostEqual(log_prob, math.log(expected), places=3)

        self._check_log_prob(Normal(loc, scale), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_normal_sample(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for loc, scale in product([-1.0, 0.0, 1.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(Normal(loc, scale),
                                        scipy.stats.norm(loc=loc, scale=scale),
                                        'Normal(mean={}, std={})'.format(loc, scale))

    def test_exponential(self):
        rate = Variable(torch.randn(5, 5).abs(), requires_grad=True)
        rate_1d = Variable(torch.randn(1).abs(), requires_grad=True)
        self.assertEqual(Exponential(rate).sample().size(), (5, 5))
        self.assertEqual(Exponential(rate).sample((7,)).size(), (7, 5, 5))
        self.assertEqual(Exponential(rate_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Exponential(rate_1d).sample().size(), (1,))
        self.assertEqual(Exponential(0.2).sample((1,)).size(), (1,))
        self.assertEqual(Exponential(50.0).sample((1,)).size(), (1,))

        self._gradcheck_log_prob(Exponential, (rate,))
        state = torch.get_rng_state()
        eps = rate.new(rate.size()).exponential_()
        torch.set_rng_state(state)
        z = Exponential(rate).rsample()
        z.backward(torch.ones_like(z))
        self.assertEqual(rate.grad, -eps / rate**2)
        rate.grad.zero_()
        self.assertEqual(z.size(), (5, 5))

        def ref_log_prob(idx, x, log_prob):
            m = rate.data.view(-1)[idx]
            expected = math.log(m) - m * x
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(Exponential(rate), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_exponential_sample(self):
        set_rng_seed(1)  # see Note [Randomized statistical tests]
        for rate in [1e-5, 1.0, 10.]:
            self._check_sampler_sampler(Exponential(rate),
                                        scipy.stats.expon(scale=1. / rate),
                                        'Exponential(rate={})'.format(rate))

    def test_laplace(self):
        loc = Variable(torch.randn(5, 5), requires_grad=True)
        scale = Variable(torch.randn(5, 5).abs(), requires_grad=True)
        loc_1d = Variable(torch.randn(1), requires_grad=True)
        scale_1d = Variable(torch.randn(1), requires_grad=True)
        loc_delta = torch.Tensor([1.0, 0.0])
        scale_delta = torch.Tensor([1e-5, 1e-5])
        self.assertEqual(Laplace(loc, scale).sample().size(), (5, 5))
        self.assertEqual(Laplace(loc, scale).sample((7,)).size(), (7, 5, 5))
        self.assertEqual(Laplace(loc_1d, scale_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Laplace(loc_1d, scale_1d).sample().size(), (1,))
        self.assertEqual(Laplace(0.2, .6).sample((1,)).size(), (1,))
        self.assertEqual(Laplace(-0.7, 50.0).sample((1,)).size(), (1,))

        # sample check for extreme value of mean, std
        set_rng_seed(0)
        self.assertEqual(Laplace(loc_delta, scale_delta).sample(sample_shape=(1, 2)),
                         torch.Tensor([[[1.0, 0.0], [1.0, 0.0]]]),
                         prec=1e-4)

        self._gradcheck_log_prob(Laplace, (loc, scale))
        self._gradcheck_log_prob(Laplace, (loc, 1.0))
        self._gradcheck_log_prob(Laplace, (0.0, scale))

        state = torch.get_rng_state()
        eps = torch.ones_like(loc).uniform_(-.5, .5)
        torch.set_rng_state(state)
        z = Laplace(loc, scale).rsample()
        z.backward(torch.ones_like(z))
        self.assertEqual(loc.grad, torch.ones_like(loc))
        self.assertEqual(scale.grad, -eps.sign() * torch.log1p(-2 * eps.abs()))
        loc.grad.zero_()
        scale.grad.zero_()
        self.assertEqual(z.size(), (5, 5))

        def ref_log_prob(idx, x, log_prob):
            m = loc.data.view(-1)[idx]
            s = scale.data.view(-1)[idx]
            expected = (-math.log(2 * s) - abs(x - m) / s)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(Laplace(loc, scale), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_laplace_sample(self):
        set_rng_seed(1)  # see Note [Randomized statistical tests]
        for loc, scale in product([-1.0, 0.0, 1.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(Laplace(loc, scale),
                                        scipy.stats.laplace(loc=loc, scale=scale),
                                        'Laplace(loc={}, scale={})'.format(loc, scale))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_gamma_shape(self):
        alpha = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        beta = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        alpha_1d = Variable(torch.exp(torch.randn(1)), requires_grad=True)
        beta_1d = Variable(torch.exp(torch.randn(1)), requires_grad=True)
        self.assertEqual(Gamma(alpha, beta).sample().size(), (2, 3))
        self.assertEqual(Gamma(alpha, beta).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(Gamma(alpha_1d, beta_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Gamma(alpha_1d, beta_1d).sample().size(), (1,))
        self.assertEqual(Gamma(0.5, 0.5).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Gamma(0.5, 0.5).sample((1,)).size(), (1,))

        def ref_log_prob(idx, x, log_prob):
            a = alpha.data.view(-1)[idx]
            b = beta.data.view(-1)[idx]
            expected = scipy.stats.gamma.logpdf(x, a, scale=1 / b)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(Gamma(alpha, beta), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_gamma_sample(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for alpha, beta in product([0.1, 1.0, 5.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(Gamma(alpha, beta),
                                        scipy.stats.gamma(alpha, scale=1.0 / beta),
                                        'Gamma(concentration={}, rate={})'.format(alpha, beta))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_pareto(self):
        scale = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        alpha = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        scale_1d = Variable(torch.randn(1).abs(), requires_grad=True)
        alpha_1d = Variable(torch.randn(1).abs(), requires_grad=True)
        self.assertEqual(Pareto(scale_1d, 0.5).mean, float('inf'), allow_inf=True)
        self.assertEqual(Pareto(scale_1d, 0.5).variance, float('inf'), allow_inf=True)
        self.assertEqual(Pareto(scale, alpha).sample().size(), (2, 3))
        self.assertEqual(Pareto(scale, alpha).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(Pareto(scale_1d, alpha_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Pareto(scale_1d, alpha_1d).sample().size(), (1,))
        self.assertEqual(Pareto(1.0, 1.0).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Pareto(1.0, 1.0).sample((1,)).size(), SCALAR_SHAPE + (1,))

        def ref_log_prob(idx, x, log_prob):
            s = scale.data.view(-1)[idx]
            a = alpha.data.view(-1)[idx]
            expected = scipy.stats.pareto.logpdf(x, a, scale=s)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(Pareto(scale, alpha), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_pareto_sample(self):
        set_rng_seed(1)  # see Note [Randomized statistical tests]
        for scale, alpha in product([0.1, 1.0, 5.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(Pareto(scale, alpha),
                                        scipy.stats.pareto(alpha, scale=scale),
                                        'Pareto(scale={}, alpha={})'.format(scale, alpha))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_gumbel(self):
        loc = Variable(torch.randn(2, 3), requires_grad=True)
        scale = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        loc_1d = Variable(torch.randn(1), requires_grad=True)
        scale_1d = Variable(torch.randn(1).abs(), requires_grad=True)
        self.assertEqual(Gumbel(loc, scale).sample().size(), (2, 3))
        self.assertEqual(Gumbel(loc, scale).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(Gumbel(loc_1d, scale_1d).sample().size(), (1,))
        self.assertEqual(Gumbel(loc_1d, scale_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Gumbel(1.0, 1.0).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Gumbel(1.0, 1.0).sample((1,)).size(), (1,))

        def ref_log_prob(idx, x, log_prob):
            l = loc.data.view(-1)[idx]
            s = scale.data.view(-1)[idx]
            expected = scipy.stats.gumbel_r.logpdf(x, loc=l, scale=s)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(Gumbel(loc, scale), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_gumbel_sample(self):
        set_rng_seed(1)  # see note [Randomized statistical tests]
        for loc, scale in product([-5.0, -1.0, -0.1, 0.1, 1.0, 5.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(Gumbel(loc, scale),
                                        scipy.stats.gumbel_r(loc=loc, scale=scale),
                                        'Gumbel(loc={}, scale={})'.format(loc, scale))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_fishersnedecor(self):
        df1 = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        df2 = Variable(torch.randn(2, 3).abs(), requires_grad=True)
        df1_1d = torch.randn(1).abs()
        df2_1d = torch.randn(1).abs()
        self.assertTrue(is_all_nan(FisherSnedecor(1, 2).mean))
        self.assertTrue(is_all_nan(FisherSnedecor(1, 4).variance))
        self.assertEqual(FisherSnedecor(df1, df2).sample().size(), (2, 3))
        self.assertEqual(FisherSnedecor(df1, df2).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(FisherSnedecor(df1_1d, df2_1d).sample().size(), (1,))
        self.assertEqual(FisherSnedecor(df1_1d, df2_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(FisherSnedecor(1.0, 1.0).sample().size(), SCALAR_SHAPE)
        self.assertEqual(FisherSnedecor(1.0, 1.0).sample((1,)).size(), (1,))

        def ref_log_prob(idx, x, log_prob):
            f1 = df1.data.view(-1)[idx]
            f2 = df2.data.view(-1)[idx]
            expected = scipy.stats.f.logpdf(x, f1, f2)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(FisherSnedecor(df1, df2), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_fishersnedecor_sample(self):
        set_rng_seed(1)  # see note [Randomized statistical tests]
        for df1, df2 in product([0.1, 0.5, 1.0, 5.0, 10.0], [0.1, 0.5, 1.0, 5.0, 10.0]):
            self._check_sampler_sampler(FisherSnedecor(df1, df2),
                                        scipy.stats.f(df1, df2),
                                        'FisherSnedecor(loc={}, scale={})'.format(df1, df2))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_chi2_shape(self):
        df = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        df_1d = Variable(torch.exp(torch.randn(1)), requires_grad=True)
        self.assertEqual(Chi2(df).sample().size(), (2, 3))
        self.assertEqual(Chi2(df).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(Chi2(df_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(Chi2(df_1d).sample().size(), (1,))
        self.assertEqual(Chi2(variable(0.5, requires_grad=True)).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Chi2(0.5).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Chi2(0.5).sample((1,)).size(), (1,))

        def ref_log_prob(idx, x, log_prob):
            d = df.data.view(-1)[idx]
            expected = scipy.stats.chi2.logpdf(x, d)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(Chi2(df), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_chi2_sample(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for df in [0.1, 1.0, 5.0]:
            self._check_sampler_sampler(Chi2(df),
                                        scipy.stats.chi2(df),
                                        'Chi2(df={})'.format(df))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_studentT(self):
        df = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        df_1d = Variable(torch.exp(torch.randn(1)), requires_grad=True)
        self.assertTrue(is_all_nan(StudentT(1).mean))
        self.assertTrue(is_all_nan(StudentT(1).variance))
        self.assertEqual(StudentT(2).variance, float('inf'), allow_inf=True)
        self.assertEqual(StudentT(df).sample().size(), (2, 3))
        self.assertEqual(StudentT(df).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(StudentT(df_1d).sample((1,)).size(), (1, 1))
        self.assertEqual(StudentT(df_1d).sample().size(), (1,))
        self.assertEqual(StudentT(variable(0.5, requires_grad=True)).sample().size(), SCALAR_SHAPE)
        self.assertEqual(StudentT(0.5).sample().size(), SCALAR_SHAPE)
        self.assertEqual(StudentT(0.5).sample((1,)).size(), (1,))

        def ref_log_prob(idx, x, log_prob):
            d = df.data.view(-1)[idx]
            expected = scipy.stats.t.logpdf(x, d)
            self.assertAlmostEqual(log_prob, expected, places=3)

        self._check_log_prob(StudentT(df), ref_log_prob)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_studentT_sample(self):
        set_rng_seed(11)  # see Note [Randomized statistical tests]
        for df, loc, scale in product([0.1, 1.0, 5.0, 10.0], [-1.0, 0.0, 1.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(StudentT(df=df, loc=loc, scale=scale),
                                        scipy.stats.t(df=df, loc=loc, scale=scale),
                                        'StudentT(df={}, loc={}, scale={})'.format(df, loc, scale))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_studentT_log_prob(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        num_samples = 10
        for df, loc, scale in product([0.1, 1.0, 5.0, 10.0], [-1.0, 0.0, 1.0], [0.1, 1.0, 10.0]):
            dist = StudentT(df=df, loc=loc, scale=scale)
            x = dist.sample((num_samples,))
            actual_log_prob = dist.log_prob(x)
            for i in range(num_samples):
                expected_log_prob = scipy.stats.t.logpdf(x[i], df=df, loc=loc, scale=scale)
                self.assertAlmostEqual(float(actual_log_prob[i]), float(expected_log_prob), places=3)

    def test_dirichlet_shape(self):
        alpha = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        alpha_1d = Variable(torch.exp(torch.randn(4)), requires_grad=True)
        self.assertEqual(Dirichlet(alpha).sample().size(), (2, 3))
        self.assertEqual(Dirichlet(alpha).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(Dirichlet(alpha_1d).sample().size(), (4,))
        self.assertEqual(Dirichlet(alpha_1d).sample((1,)).size(), (1, 4))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_dirichlet_log_prob(self):
        num_samples = 10
        alpha = torch.exp(torch.randn(5))
        dist = Dirichlet(alpha)
        x = dist.sample((num_samples,))
        actual_log_prob = dist.log_prob(x)
        for i in range(num_samples):
            expected_log_prob = scipy.stats.dirichlet.logpdf(x[i].numpy(), alpha.numpy())
            self.assertAlmostEqual(actual_log_prob[i], expected_log_prob, places=3)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_dirichlet_sample(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        alpha = torch.exp(torch.randn(3))
        self._check_sampler_sampler(Dirichlet(alpha),
                                    scipy.stats.dirichlet(alpha.numpy()),
                                    'Dirichlet(alpha={})'.format(list(alpha)),
                                    multivariate=True)

    def test_beta_shape(self):
        con1 = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        con0 = Variable(torch.exp(torch.randn(2, 3)), requires_grad=True)
        con1_1d = Variable(torch.exp(torch.randn(4)), requires_grad=True)
        con0_1d = Variable(torch.exp(torch.randn(4)), requires_grad=True)
        self.assertEqual(Beta(con1, con0).sample().size(), (2, 3))
        self.assertEqual(Beta(con1, con0).sample((5,)).size(), (5, 2, 3))
        self.assertEqual(Beta(con1_1d, con0_1d).sample().size(), (4,))
        self.assertEqual(Beta(con1_1d, con0_1d).sample((1,)).size(), (1, 4))
        self.assertEqual(Beta(0.1, 0.3).sample().size(), SCALAR_SHAPE)
        self.assertEqual(Beta(0.1, 0.3).sample((5,)).size(), (5,))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_beta_log_prob(self):
        for _ in range(100):
            con1 = np.exp(np.random.normal())
            con0 = np.exp(np.random.normal())
            dist = Beta(con1, con0)
            x = dist.sample()
            actual_log_prob = dist.log_prob(x).sum()
            expected_log_prob = scipy.stats.beta.logpdf(x, con1, con0)
            self.assertAlmostEqual(float(actual_log_prob), float(expected_log_prob), places=3, allow_inf=True)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_beta_sample(self):
        set_rng_seed(1)  # see Note [Randomized statistical tests]
        for con1, con0 in product([0.1, 1.0, 10.0], [0.1, 1.0, 10.0]):
            self._check_sampler_sampler(Beta(con1, con0),
                                        scipy.stats.beta(con1, con0),
                                        'Beta(alpha={}, beta={})'.format(con1, con0))
        # Check that small alphas do not cause NANs.
        for Tensor in [torch.FloatTensor, torch.DoubleTensor]:
            x = Beta(Tensor([1e-6]), Tensor([1e-6])).sample()[0]
            self.assertTrue(np.isfinite(x) and x > 0, 'Invalid Beta.sample(): {}'.format(x))

    def test_cdf_icdf_inverse(self):
        # Tests the invertibility property on the distributions
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                samples = dist.sample(sample_shape=(20,))
                try:
                    cdf = dist.cdf(samples)
                    actual = dist.icdf(cdf)
                except NotImplementedError:
                    continue
                rel_error = torch.abs(actual - samples) / (1e-10 + torch.abs(samples))
                self.assertLess(rel_error.max(), 1e-4, msg='\n'.join([
                    '{} example {}/{}, icdf(cdf(x)) != x'.format(Dist.__name__, i + 1, len(params)),
                    'x = {}'.format(samples),
                    'cdf(x) = {}'.format(cdf),
                    'icdf(cdf(x)) = {}'.format(actual),
                ]))

    def test_cdf_log_prob(self):
        # Tests if the differentiation of the CDF gives the PDF at a given value
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                samples = Variable(dist.sample().data, requires_grad=True)
                try:
                    cdfs = dist.cdf(samples)
                    pdfs = dist.log_prob(samples).exp()
                except NotImplementedError:
                    continue
                cdfs_derivative = grad(cdfs.sum(), [samples])[0]  # this should not be wrapped in torch.abs()
                self.assertEqual(cdfs_derivative, pdfs, message='\n'.join([
                    '{} example {}/{}, d(cdf)/dx != pdf(x)'.format(Dist.__name__, i + 1, len(params)),
                    'x = {}'.format(samples),
                    'cdf = {}'.format(cdfs),
                    'pdf = {}'.format(pdfs),
                    'grad(cdf) = {}'.format(cdfs_derivative),
                ]))

    def test_valid_parameter_broadcasting(self):
        # Test correct broadcasting of parameter sizes for distributions that have multiple
        # parameters.
        # example type (distribution instance, expected sample shape)
        valid_examples = [
            (Normal(loc=variable([0, 0]), scale=1),
             (2,)),
            (Normal(loc=0, scale=variable([1, 1])),
             (2,)),
            (Normal(loc=variable([0, 0]), scale=variable([1])),
             (2,)),
            (Normal(loc=variable([0, 0]), scale=variable([[1], [1]])),
             (2, 2)),
            (Normal(loc=variable([0, 0]), scale=variable([[1]])),
             (1, 2)),
            (Normal(loc=variable([0]), scale=variable([[1]])),
             (1, 1)),
            (FisherSnedecor(df1=variable([1, 1]), df2=1),
             (2,)),
            (FisherSnedecor(df1=1, df2=variable([1, 1])),
             (2,)),
            (FisherSnedecor(df1=variable([1, 1]), df2=variable([1])),
             (2,)),
            (FisherSnedecor(df1=variable([1, 1]), df2=variable([[1], [1]])),
             (2, 2)),
            (FisherSnedecor(df1=variable([1, 1]), df2=variable([[1]])),
             (1, 2)),
            (FisherSnedecor(df1=variable([1]), df2=variable([[1]])),
             (1, 1)),
            (Gamma(concentration=variable([1, 1]), rate=1),
             (2,)),
            (Gamma(concentration=1, rate=variable([1, 1])),
             (2,)),
            (Gamma(concentration=variable([1, 1]), rate=variable([[1], [1], [1]])),
             (3, 2)),
            (Gamma(concentration=variable([1, 1]), rate=variable([[1], [1]])),
             (2, 2)),
            (Gamma(concentration=variable([1, 1]), rate=variable([[1]])),
             (1, 2)),
            (Gamma(concentration=variable([1]), rate=variable([[1]])),
             (1, 1)),
            (Gumbel(loc=variable([0, 0]), scale=1),
             (2,)),
            (Gumbel(loc=0, scale=variable([1, 1])),
             (2,)),
            (Gumbel(loc=variable([0, 0]), scale=variable([1])),
             (2,)),
            (Gumbel(loc=variable([0, 0]), scale=variable([[1], [1]])),
             (2, 2)),
            (Gumbel(loc=variable([0, 0]), scale=variable([[1]])),
             (1, 2)),
            (Gumbel(loc=variable([0]), scale=variable([[1]])),
             (1, 1)),
            (Laplace(loc=variable([0, 0]), scale=1),
             (2,)),
            (Laplace(loc=0, scale=variable([1, 1])),
             (2,)),
            (Laplace(loc=variable([0, 0]), scale=variable([1])),
             (2,)),
            (Laplace(loc=variable([0, 0]), scale=variable([[1], [1]])),
             (2, 2)),
            (Laplace(loc=variable([0, 0]), scale=variable([[1]])),
             (1, 2)),
            (Laplace(loc=variable([0]), scale=variable([[1]])),
             (1, 1)),
            (Pareto(scale=variable([1, 1]), alpha=1),
             (2,)),
            (Pareto(scale=1, alpha=variable([1, 1])),
             (2,)),
            (Pareto(scale=variable([1, 1]), alpha=variable([1])),
             (2,)),
            (Pareto(scale=variable([1, 1]), alpha=variable([[1], [1]])),
             (2, 2)),
            (Pareto(scale=variable([1, 1]), alpha=variable([[1]])),
             (1, 2)),
            (Pareto(scale=variable([1]), alpha=variable([[1]])),
             (1, 1)),
            (StudentT(df=variable([1, 1]), loc=1),
             (2,)),
            (StudentT(df=1, scale=variable([1, 1])),
             (2,)),
            (StudentT(df=variable([1, 1]), loc=variable([1])),
             (2,)),
            (StudentT(df=variable([1, 1]), scale=variable([[1], [1]])),
             (2, 2)),
            (StudentT(df=variable([1, 1]), loc=variable([[1]])),
             (1, 2)),
            (StudentT(df=variable([1]), scale=variable([[1]])),
             (1, 1)),
        ]

        for dist, expected_size in valid_examples:
            dist_sample_size = dist.sample().size()
            self.assertEqual(dist_sample_size, expected_size,
                             'actual size: {} != expected size: {}'.format(dist_sample_size, expected_size))

    def test_invalid_parameter_broadcasting(self):
        # invalid broadcasting cases; should throw error
        # example type (distribution class, distribution params)
        invalid_examples = [
            (Normal, {
                'loc': variable([[0, 0]]),
                'scale': variable([1, 1, 1, 1])
            }),
            (Normal, {
                'loc': variable([[[0, 0, 0], [0, 0, 0]]]),
                'scale': variable([1, 1])
            }),
            (FisherSnedecor, {
                'df1': variable([1, 1]),
                'df2': variable([1, 1, 1]),
            }),
            (Gumbel, {
                'loc': variable([[0, 0]]),
                'scale': variable([1, 1, 1, 1])
            }),
            (Gumbel, {
                'loc': variable([[[0, 0, 0], [0, 0, 0]]]),
                'scale': variable([1, 1])
            }),
            (Gamma, {
                'concentration': variable([0, 0]),
                'rate': variable([1, 1, 1])
            }),
            (Laplace, {
                'loc': variable([0, 0]),
                'scale': variable([1, 1, 1])
            }),
            (Pareto, {
                'scale': variable([1, 1]),
                'alpha': variable([1, 1, 1])
            }),
            (StudentT, {
                'df': variable([1, 1]),
                'scale': variable([1, 1, 1])
            }),
            (StudentT, {
                'df': variable([1, 1]),
                'loc': variable([1, 1, 1])
            })
        ]

        for dist, kwargs in invalid_examples:
            self.assertRaises(RuntimeError, dist, **kwargs)


# These tests are only needed for a few distributions that implement custom
# reparameterized gradients. Most .rsample() implementations simply rely on
# the reparameterization trick and do not need to be tested for accuracy.
class TestRsample(TestCase):
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_gamma(self):
        num_samples = 100
        for alpha in [1e-2, 1e-1, 1e0, 1e1, 1e2, 1e3, 1e4]:
            alphas = Variable(torch.FloatTensor([alpha] * num_samples), requires_grad=True)
            betas = Variable(torch.ones(num_samples).type_as(alphas))
            x = Gamma(alphas, betas).rsample()
            x.sum().backward()
            x, ind = x.data.sort()
            x = x.numpy()
            actual_grad = alphas.grad.data[ind].numpy()
            # Compare with expected gradient dx/dalpha along constant cdf(x,alpha).
            cdf = scipy.stats.gamma.cdf
            pdf = scipy.stats.gamma.pdf
            eps = 0.01 * alpha / (1.0 + alpha ** 0.5)
            cdf_alpha = (cdf(x, alpha + eps) - cdf(x, alpha - eps)) / (2 * eps)
            cdf_x = pdf(x, alpha)
            expected_grad = -cdf_alpha / cdf_x
            rel_error = np.abs(actual_grad - expected_grad) / (expected_grad + 1e-30)
            self.assertLess(np.max(rel_error), 0.0005, '\n'.join([
                'Bad gradient dx/alpha for x ~ Gamma({}, 1)'.format(alpha),
                'x {}'.format(x),
                'expected {}'.format(expected_grad),
                'actual {}'.format(actual_grad),
                'rel error {}'.format(rel_error),
                'max error {}'.format(rel_error.max()),
                'at alpha={}, x={}'.format(alpha, x[rel_error.argmax()]),
            ]))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_chi2(self):
        num_samples = 100
        for df in [1e-2, 1e-1, 1e0, 1e1, 1e2, 1e3, 1e4]:
            dfs = Variable(torch.FloatTensor([df] * num_samples), requires_grad=True)
            x = Chi2(dfs).rsample()
            x.sum().backward()
            x, ind = x.data.sort()
            x = x.numpy()
            actual_grad = dfs.grad.data[ind].numpy()
            # Compare with expected gradient dx/ddf along constant cdf(x,df).
            cdf = scipy.stats.chi2.cdf
            pdf = scipy.stats.chi2.pdf
            eps = 0.01 * df / (1.0 + df ** 0.5)
            cdf_df = (cdf(x, df + eps) - cdf(x, df - eps)) / (2 * eps)
            cdf_x = pdf(x, df)
            expected_grad = -cdf_df / cdf_x
            rel_error = np.abs(actual_grad - expected_grad) / (expected_grad + 1e-30)
            self.assertLess(np.max(rel_error), 0.001, '\n'.join([
                'Bad gradient dx/ddf for x ~ Chi2({})'.format(df),
                'x {}'.format(x),
                'expected {}'.format(expected_grad),
                'actual {}'.format(actual_grad),
                'rel error {}'.format(rel_error),
                'max error {}'.format(rel_error.max()),
            ]))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_dirichlet_on_diagonal(self):
        num_samples = 20
        grid = [1e-1, 1e0, 1e1]
        for a0, a1, a2 in product(grid, grid, grid):
            alphas = Variable(torch.FloatTensor([[a0, a1, a2]] * num_samples), requires_grad=True)
            x = Dirichlet(alphas).rsample()[:, 0]
            x.sum().backward()
            x, ind = x.data.sort()
            x = x.numpy()
            actual_grad = alphas.grad.data[ind].numpy()[:, 0]
            # Compare with expected gradient dx/dalpha0 along constant cdf(x,alpha).
            # This reduces to a distribution Beta(alpha[0], alpha[1] + alpha[2]).
            cdf = scipy.stats.beta.cdf
            pdf = scipy.stats.beta.pdf
            alpha, beta = a0, a1 + a2
            eps = 0.01 * alpha / (1.0 + np.sqrt(alpha))
            cdf_alpha = (cdf(x, alpha + eps, beta) - cdf(x, alpha - eps, beta)) / (2 * eps)
            cdf_x = pdf(x, alpha, beta)
            expected_grad = -cdf_alpha / cdf_x
            rel_error = np.abs(actual_grad - expected_grad) / (expected_grad + 1e-30)
            self.assertLess(np.max(rel_error), 0.001, '\n'.join([
                'Bad gradient dx[0]/dalpha[0] for Dirichlet([{}, {}, {}])'.format(a0, a1, a2),
                'x {}'.format(x),
                'expected {}'.format(expected_grad),
                'actual {}'.format(actual_grad),
                'rel error {}'.format(rel_error),
                'max error {}'.format(rel_error.max()),
                'at x={}'.format(x[rel_error.argmax()]),
            ]))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_beta_wrt_alpha(self):
        num_samples = 20
        grid = [1e-2, 1e-1, 1e0, 1e1, 1e2]
        for con1, con0 in product(grid, grid):
            con1s = Variable(torch.FloatTensor([con1] * num_samples), requires_grad=True)
            con0s = Variable(torch.FloatTensor([con0] * num_samples).type_as(con1s))
            x = Beta(con1s, con0s).rsample()
            x.sum().backward()
            x, ind = x.data.sort()
            x = x.numpy()
            actual_grad = con1s.grad.data[ind].numpy()
            # Compare with expected gradient dx/dcon1 along constant cdf(x,con1,con0).
            cdf = scipy.stats.beta.cdf
            pdf = scipy.stats.beta.pdf
            eps = 0.01 * con1 / (1.0 + np.sqrt(con1))
            cdf_alpha = (cdf(x, con1 + eps, con0) - cdf(x, con1 - eps, con0)) / (2 * eps)
            cdf_x = pdf(x, con1, con0)
            expected_grad = -cdf_alpha / cdf_x
            rel_error = np.abs(actual_grad - expected_grad) / (expected_grad + 1e-30)
            self.assertLess(np.max(rel_error), 0.005, '\n'.join([
                'Bad gradient dx/dcon1 for x ~ Beta({}, {})'.format(con1, con0),
                'x {}'.format(x),
                'expected {}'.format(expected_grad),
                'actual {}'.format(actual_grad),
                'rel error {}'.format(rel_error),
                'max error {}'.format(rel_error.max()),
                'at x = {}'.format(x[rel_error.argmax()]),
            ]))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_beta_wrt_beta(self):
        num_samples = 20
        grid = [1e-2, 1e-1, 1e0, 1e1, 1e2]
        for con1, con0 in product(grid, grid):
            con0s = Variable(torch.FloatTensor([con0] * num_samples), requires_grad=True)
            con1s = Variable(torch.FloatTensor([con1] * num_samples).type_as(con0s))
            x = Beta(con1s, con0s).rsample()
            x.sum().backward()
            x, ind = x.data.sort()
            x = x.numpy()
            actual_grad = con0s.grad.data[ind].numpy()
            # Compare with expected gradient dx/dcon0 along constant cdf(x,con1,con0).
            cdf = scipy.stats.beta.cdf
            pdf = scipy.stats.beta.pdf
            eps = 0.01 * con0 / (1.0 + np.sqrt(con0))
            cdf_beta = (cdf(x, con1, con0 + eps) - cdf(x, con1, con0 - eps)) / (2 * eps)
            cdf_x = pdf(x, con1, con0)
            expected_grad = -cdf_beta / cdf_x
            rel_error = np.abs(actual_grad - expected_grad) / (expected_grad + 1e-30)
            self.assertLess(np.max(rel_error), 0.005, '\n'.join([
                'Bad gradient dx/dcon0 for x ~ Beta({}, {})'.format(con1, con0),
                'x {}'.format(x),
                'expected {}'.format(expected_grad),
                'actual {}'.format(actual_grad),
                'rel error {}'.format(rel_error),
                'max error {}'.format(rel_error.max()),
                'at x = {!r}'.format(x[rel_error.argmax()]),
            ]))

    def test_dirichlet_multivariate(self):
        alpha_crit = 0.25 * (5.0 ** 0.5 - 1.0)
        num_samples = 100000
        for shift in [-0.1, -0.05, -0.01, 0.0, 0.01, 0.05, 0.10]:
            alpha = alpha_crit + shift
            alpha = Variable(torch.FloatTensor([alpha]), requires_grad=True)
            alpha_vec = torch.cat([alpha, alpha, alpha.new([1])])
            z = Dirichlet(alpha_vec.expand(num_samples, 3)).rsample()
            mean_z3 = 1.0 / (2.0 * alpha + 1.0)
            loss = torch.pow(z[:, 2] - mean_z3, 2.0).mean()
            actual_grad = grad(loss, [alpha])[0].data
            # Compute expected gradient by hand.
            num = 1.0 - 2.0 * alpha - 4.0 * alpha**2
            den = (1.0 + alpha)**2 * (1.0 + 2.0 * alpha)**3
            expected_grad = (num / den).data
            self.assertEqual(actual_grad, expected_grad, 0.002, '\n'.join([
                "alpha = alpha_c + %.2g" % shift,
                "expected_grad: %.5g" % expected_grad,
                "actual_grad: %.5g" % actual_grad,
                "error = %.2g" % torch.abs(expected_grad - actual_grad).max(),
            ]))

    def test_dirichlet_tangent_field(self):
        num_samples = 20
        alpha_grid = [0.5, 1.0, 2.0]

        # v = dx/dalpha[0] is the reparameterized gradient aka tangent field.
        def compute_v(x, alpha):
            return torch.stack([
                _Dirichlet_backward(x, alpha, torch.eye(3, 3)[i].expand_as(x))[:, 0]
                for i in range(3)
            ], dim=-1)

        for a1, a2, a3 in product(alpha_grid, alpha_grid, alpha_grid):
            alpha = Variable(torch.Tensor([a1, a2, a3]).expand(num_samples, 3), requires_grad=True)
            x = Dirichlet(alpha).rsample()
            dlogp_da = grad([Dirichlet(alpha).log_prob(x.detach()).sum()],
                            [alpha], retain_graph=True)[0].data[:, 0]
            dlogp_dx = grad([Dirichlet(alpha.detach()).log_prob(x).sum()],
                            [x], retain_graph=True)[0].data
            v = torch.stack([grad([x[:, i].sum()], [alpha], retain_graph=True)[0].data[:, 0]
                             for i in range(3)], dim=-1)
            # Compute ramaining properties by finite difference.
            x = x.data
            alpha = alpha.data
            self.assertEqual(compute_v(x, alpha), v, message='Bug in compute_v() helper')
            # dx is an arbitrary orthonormal basis tangent to the simplex.
            dx = torch.Tensor([[2, -1, -1], [0, 1, -1]])
            dx /= dx.norm(2, -1, True)
            eps = 1e-2 * x.min(-1, True)[0]  # avoid boundary
            dv0 = (compute_v(x + eps * dx[0], alpha) - compute_v(x - eps * dx[0], alpha)) / (2 * eps)
            dv1 = (compute_v(x + eps * dx[1], alpha) - compute_v(x - eps * dx[1], alpha)) / (2 * eps)
            div_v = (dv0 * dx[0] + dv1 * dx[1]).sum(-1)
            # This is a modification of the standard continuity equation, using the product rule to allow
            # expression in terms of log_prob rather than the less numerically stable log_prob.exp().
            error = dlogp_da + (dlogp_dx * v).sum(-1) + div_v
            self.assertLess(torch.abs(error).max(), 0.005, '\n'.join([
                'Dirichlet([{}, {}, {}]) gradient violates continuity equation:'.format(a1, a2, a3),
                'error = {}'.format(error),
            ]))


class TestDistributionShapes(TestCase):
    def setUp(self):
        super(TestCase, self).setUp()
        self.scalar_sample = 1
        self.tensor_sample_1 = Variable(torch.ones(3, 2))
        self.tensor_sample_2 = Variable(torch.ones(3, 2, 3))

    def test_entropy_shape(self):
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                try:
                    actual_shape = dist.entropy().size()
                    expected_shape = dist.batch_shape if dist.batch_shape else torch.Size(SCALAR_SHAPE)
                    message = '{} example {}/{}, shape mismatch. expected {}, actual {}'.format(
                        Dist.__name__, i + 1, len(params), expected_shape, actual_shape)
                    self.assertEqual(actual_shape, expected_shape, message=message)
                except NotImplementedError:
                    continue

    def test_bernoulli_shape_scalar_params(self):
        bernoulli = Bernoulli(0.3)
        self.assertEqual(bernoulli._batch_shape, torch.Size())
        self.assertEqual(bernoulli._event_shape, torch.Size())
        self.assertEqual(bernoulli.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(bernoulli.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, bernoulli.log_prob, self.scalar_sample)
        self.assertEqual(bernoulli.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(bernoulli.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_bernoulli_shape_tensor_params(self):
        bernoulli = Bernoulli(variable([[0.6, 0.3], [0.6, 0.3], [0.6, 0.3]]))
        self.assertEqual(bernoulli._batch_shape, torch.Size((3, 2)))
        self.assertEqual(bernoulli._event_shape, torch.Size(()))
        self.assertEqual(bernoulli.sample().size(), torch.Size((3, 2)))
        self.assertEqual(bernoulli.sample((3, 2)).size(), torch.Size((3, 2, 3, 2)))
        self.assertEqual(bernoulli.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, bernoulli.log_prob, self.tensor_sample_2)
        self.assertEqual(bernoulli.log_prob(Variable(torch.ones(3, 1, 1))).size(), torch.Size((3, 3, 2)))

    def test_geometric_shape_scalar_params(self):
        geometric = Geometric(0.3)
        self.assertEqual(geometric._batch_shape, torch.Size())
        self.assertEqual(geometric._event_shape, torch.Size())
        self.assertEqual(geometric.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(geometric.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, geometric.log_prob, self.scalar_sample)
        self.assertEqual(geometric.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(geometric.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_geometric_shape_tensor_params(self):
        geometric = Geometric(variable([[0.6, 0.3], [0.6, 0.3], [0.6, 0.3]]))
        self.assertEqual(geometric._batch_shape, torch.Size((3, 2)))
        self.assertEqual(geometric._event_shape, torch.Size(()))
        self.assertEqual(geometric.sample().size(), torch.Size((3, 2)))
        self.assertEqual(geometric.sample((3, 2)).size(), torch.Size((3, 2, 3, 2)))
        self.assertEqual(geometric.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, geometric.log_prob, self.tensor_sample_2)
        self.assertEqual(geometric.log_prob(Variable(torch.ones(3, 1, 1))).size(), torch.Size((3, 3, 2)))

    def test_beta_shape_scalar_params(self):
        dist = Beta(0.1, 0.1)
        self.assertEqual(dist._batch_shape, torch.Size())
        self.assertEqual(dist._event_shape, torch.Size())
        self.assertEqual(dist.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, dist.log_prob, self.scalar_sample)
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_beta_shape_tensor_params(self):
        dist = Beta(variable([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]),
                    variable([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]))
        self.assertEqual(dist._batch_shape, torch.Size((3, 2)))
        self.assertEqual(dist._event_shape, torch.Size(()))
        self.assertEqual(dist.sample().size(), torch.Size((3, 2)))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2, 3, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_2)
        self.assertEqual(dist.log_prob(Variable(torch.ones(3, 1, 1))).size(), torch.Size((3, 3, 2)))

    def test_binomial_shape(self):
        dist = Binomial(10, variable([0.6, 0.3]))
        self.assertEqual(dist._batch_shape, torch.Size((2,)))
        self.assertEqual(dist._event_shape, torch.Size(()))
        self.assertEqual(dist.sample().size(), torch.Size((2,)))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_2)

    def test_multinomial_shape(self):
        dist = Multinomial(10, variable([[0.6, 0.3], [0.6, 0.3], [0.6, 0.3]]))
        self.assertEqual(dist._batch_shape, torch.Size((3,)))
        self.assertEqual(dist._event_shape, torch.Size((2,)))
        self.assertEqual(dist.sample().size(), torch.Size((3, 2)))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2, 3, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3,)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_2)
        self.assertEqual(dist.log_prob(Variable(torch.ones(3, 1, 2))).size(), torch.Size((3, 3)))

    def test_categorical_shape(self):
        # unbatched
        dist = Categorical(variable([0.6, 0.3, 0.1]))
        self.assertEqual(dist._batch_shape, torch.Size(()))
        self.assertEqual(dist._event_shape, torch.Size(()))
        self.assertEqual(dist.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2,)))
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))
        self.assertEqual(dist.log_prob(Variable(torch.ones(3, 1))).size(), torch.Size((3, 1)))
        # batched
        dist = Categorical(variable([[0.6, 0.3], [0.6, 0.3], [0.6, 0.3]]))
        self.assertEqual(dist._batch_shape, torch.Size((3,)))
        self.assertEqual(dist._event_shape, torch.Size(()))
        self.assertEqual(dist.sample().size(), torch.Size((3,)))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2, 3,)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_1)
        self.assertEqual(dist.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))
        self.assertEqual(dist.log_prob(Variable(torch.ones(3, 1))).size(), torch.Size((3, 3)))

    def test_one_hot_categorical_shape(self):
        # unbatched
        dist = OneHotCategorical(variable([0.6, 0.3, 0.1]))
        self.assertEqual(dist._batch_shape, torch.Size(()))
        self.assertEqual(dist._event_shape, torch.Size((3,)))
        self.assertEqual(dist.sample().size(), torch.Size((3,)))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2, 3)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_1)
        self.assertEqual(dist.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2,)))
        self.assertEqual(dist.log_prob(dist.enumerate_support()).size(), torch.Size((3,)))
        self.assertEqual(dist.log_prob(Variable(torch.ones(3, 3))).size(), torch.Size((3,)))
        # batched
        dist = OneHotCategorical(variable([[0.6, 0.3], [0.6, 0.3], [0.6, 0.3]]))
        self.assertEqual(dist._batch_shape, torch.Size((3,)))
        self.assertEqual(dist._event_shape, torch.Size((2,)))
        self.assertEqual(dist.sample().size(), torch.Size((3, 2)))
        self.assertEqual(dist.sample((3, 2)).size(), torch.Size((3, 2, 3, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3,)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_2)
        self.assertEqual(dist.log_prob(dist.enumerate_support()).size(), torch.Size((2, 3)))
        self.assertEqual(dist.log_prob(Variable(torch.ones((3, 1, 2)))).size(), torch.Size((3, 3)))

    def test_cauchy_shape_scalar_params(self):
        cauchy = Cauchy(0, 1)
        self.assertEqual(cauchy._batch_shape, torch.Size())
        self.assertEqual(cauchy._event_shape, torch.Size())
        self.assertEqual(cauchy.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(cauchy.sample(torch.Size((3, 2))).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, cauchy.log_prob, self.scalar_sample)
        self.assertEqual(cauchy.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(cauchy.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_cauchy_shape_tensor_params(self):
        cauchy = Cauchy(variable([0, 0]), variable([1, 1]))
        self.assertEqual(cauchy._batch_shape, torch.Size((2,)))
        self.assertEqual(cauchy._event_shape, torch.Size(()))
        self.assertEqual(cauchy.sample().size(), torch.Size((2,)))
        self.assertEqual(cauchy.sample(torch.Size((3, 2))).size(), torch.Size((3, 2, 2)))
        self.assertEqual(cauchy.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, cauchy.log_prob, self.tensor_sample_2)
        self.assertEqual(cauchy.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))

    def test_dirichlet_shape(self):
        dist = Dirichlet(variable([[0.6, 0.3], [1.6, 1.3], [2.6, 2.3]]))
        self.assertEqual(dist._batch_shape, torch.Size((3,)))
        self.assertEqual(dist._event_shape, torch.Size((2,)))
        self.assertEqual(dist.sample().size(), torch.Size((3, 2)))
        self.assertEqual(dist.sample((5, 4)).size(), torch.Size((5, 4, 3, 2)))
        self.assertEqual(dist.log_prob(self.tensor_sample_1).size(), torch.Size((3,)))
        self.assertRaises(ValueError, dist.log_prob, self.tensor_sample_2)
        self.assertEqual(dist.log_prob(Variable(torch.ones((3, 1, 2)))).size(), torch.Size((3, 3)))

    def test_gamma_shape_scalar_params(self):
        gamma = Gamma(1, 1)
        self.assertEqual(gamma._batch_shape, torch.Size())
        self.assertEqual(gamma._event_shape, torch.Size())
        self.assertEqual(gamma.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(gamma.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, gamma.log_prob, self.scalar_sample)
        self.assertEqual(gamma.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(gamma.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_gamma_shape_tensor_params(self):
        gamma = Gamma(variable([1, 1]), variable([1, 1]))
        self.assertEqual(gamma._batch_shape, torch.Size((2,)))
        self.assertEqual(gamma._event_shape, torch.Size(()))
        self.assertEqual(gamma.sample().size(), torch.Size((2,)))
        self.assertEqual(gamma.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(gamma.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, gamma.log_prob, self.tensor_sample_2)
        self.assertEqual(gamma.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))

    def test_chi2_shape_scalar_params(self):
        chi2 = Chi2(1)
        self.assertEqual(chi2._batch_shape, torch.Size())
        self.assertEqual(chi2._event_shape, torch.Size())
        self.assertEqual(chi2.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(chi2.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, chi2.log_prob, self.scalar_sample)
        self.assertEqual(chi2.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(chi2.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_chi2_shape_tensor_params(self):
        chi2 = Chi2(variable([1, 1]))
        self.assertEqual(chi2._batch_shape, torch.Size((2,)))
        self.assertEqual(chi2._event_shape, torch.Size(()))
        self.assertEqual(chi2.sample().size(), torch.Size((2,)))
        self.assertEqual(chi2.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(chi2.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, chi2.log_prob, self.tensor_sample_2)
        self.assertEqual(chi2.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))

    def test_studentT_shape_scalar_params(self):
        st = StudentT(1)
        self.assertEqual(st._batch_shape, torch.Size())
        self.assertEqual(st._event_shape, torch.Size())
        self.assertEqual(st.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(st.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, st.log_prob, self.scalar_sample)
        self.assertEqual(st.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(st.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_studentT_shape_tensor_params(self):
        st = StudentT(variable([1, 1]))
        self.assertEqual(st._batch_shape, torch.Size((2,)))
        self.assertEqual(st._event_shape, torch.Size(()))
        self.assertEqual(st.sample().size(), torch.Size((2,)))
        self.assertEqual(st.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(st.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, st.log_prob, self.tensor_sample_2)
        self.assertEqual(st.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))

    def test_pareto_shape_scalar_params(self):
        pareto = Pareto(1, 1)
        self.assertEqual(pareto._batch_shape, torch.Size(SCALAR_SHAPE))
        self.assertEqual(pareto._event_shape, torch.Size())
        self.assertEqual(pareto.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(pareto.sample((3, 2)).size(), torch.Size((3, 2) + SCALAR_SHAPE))
        self.assertRaises(ValueError, pareto.log_prob, self.scalar_sample)
        self.assertEqual(pareto.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(pareto.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_gumbel_shape_scalar_params(self):
        gumbel = Gumbel(1, 1)
        self.assertEqual(gumbel._batch_shape, torch.Size())
        self.assertEqual(gumbel._event_shape, torch.Size())
        self.assertEqual(gumbel.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(gumbel.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, gumbel.log_prob, self.scalar_sample)
        self.assertEqual(gumbel.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(gumbel.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_normal_shape_scalar_params(self):
        normal = Normal(0, 1)
        self.assertEqual(normal._batch_shape, torch.Size())
        self.assertEqual(normal._event_shape, torch.Size())
        self.assertEqual(normal.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(normal.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, normal.log_prob, self.scalar_sample)
        self.assertEqual(normal.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(normal.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_normal_shape_tensor_params(self):
        normal = Normal(variable([0, 0]), variable([1, 1]))
        self.assertEqual(normal._batch_shape, torch.Size((2,)))
        self.assertEqual(normal._event_shape, torch.Size(()))
        self.assertEqual(normal.sample().size(), torch.Size((2,)))
        self.assertEqual(normal.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(normal.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, normal.log_prob, self.tensor_sample_2)
        self.assertEqual(normal.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))

    def test_uniform_shape_scalar_params(self):
        uniform = Uniform(0, 1)
        self.assertEqual(uniform._batch_shape, torch.Size())
        self.assertEqual(uniform._event_shape, torch.Size())
        self.assertEqual(uniform.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(uniform.sample(torch.Size((3, 2))).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, uniform.log_prob, self.scalar_sample)
        self.assertEqual(uniform.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(uniform.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_uniform_shape_tensor_params(self):
        uniform = Uniform(variable([0, 0]), variable([1, 1]))
        self.assertEqual(uniform._batch_shape, torch.Size((2,)))
        self.assertEqual(uniform._event_shape, torch.Size(()))
        self.assertEqual(uniform.sample().size(), torch.Size((2,)))
        self.assertEqual(uniform.sample(torch.Size((3, 2))).size(), torch.Size((3, 2, 2)))
        self.assertEqual(uniform.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, uniform.log_prob, self.tensor_sample_2)
        self.assertEqual(uniform.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))

    def test_exponential_shape_scalar_param(self):
        expon = Exponential(1.)
        self.assertEqual(expon._batch_shape, torch.Size())
        self.assertEqual(expon._event_shape, torch.Size())
        self.assertEqual(expon.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(expon.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, expon.log_prob, self.scalar_sample)
        self.assertEqual(expon.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(expon.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_exponential_shape_tensor_param(self):
        expon = Exponential(variable([1, 1]))
        self.assertEqual(expon._batch_shape, torch.Size((2,)))
        self.assertEqual(expon._event_shape, torch.Size(()))
        self.assertEqual(expon.sample().size(), torch.Size((2,)))
        self.assertEqual(expon.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(expon.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, expon.log_prob, self.tensor_sample_2)
        self.assertEqual(expon.log_prob(Variable(torch.ones(2, 2))).size(), torch.Size((2, 2)))

    def test_laplace_shape_scalar_params(self):
        laplace = Laplace(0, 1)
        self.assertEqual(laplace._batch_shape, torch.Size())
        self.assertEqual(laplace._event_shape, torch.Size())
        self.assertEqual(laplace.sample().size(), torch.Size(SCALAR_SHAPE))
        self.assertEqual(laplace.sample((3, 2)).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, laplace.log_prob, self.scalar_sample)
        self.assertEqual(laplace.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertEqual(laplace.log_prob(self.tensor_sample_2).size(), torch.Size((3, 2, 3)))

    def test_laplace_shape_tensor_params(self):
        laplace = Laplace(variable([0, 0]), variable([1, 1]))
        self.assertEqual(laplace._batch_shape, torch.Size((2,)))
        self.assertEqual(laplace._event_shape, torch.Size(()))
        self.assertEqual(laplace.sample().size(), torch.Size((2,)))
        self.assertEqual(laplace.sample((3, 2)).size(), torch.Size((3, 2, 2)))
        self.assertEqual(laplace.log_prob(self.tensor_sample_1).size(), torch.Size((3, 2)))
        self.assertRaises(ValueError, laplace.log_prob, self.tensor_sample_2)
        self.assertEqual(laplace.log_prob(Variable(torch.ones(2, 1))).size(), torch.Size((2, 2)))


class TestKL(TestCase):

    def setUp(self):

        class Binomial30(Binomial):
            def __init__(self, probs):
                super(Binomial30, self).__init__(30, probs)

        # These are pairs of distributions with 4 x 4 parameters as specified.
        # The first of the pair e.g. bernoulli[0] varies column-wise and the second
        # e.g. bernoulli[1] varies row-wise; that way we test all param pairs.
        bernoulli = pairwise(Bernoulli, [0.1, 0.2, 0.6, 0.9])
        binomial30 = pairwise(Binomial30, [0.1, 0.2, 0.6, 0.9])
        beta = pairwise(Beta, [1.0, 2.5, 1.0, 2.5], [1.5, 1.5, 3.5, 3.5])
        categorical = pairwise(Categorical, [[0.4, 0.3, 0.3],
                                             [0.2, 0.7, 0.1],
                                             [0.33, 0.33, 0.34],
                                             [0.2, 0.2, 0.6]])
        chi2 = pairwise(Chi2, [1.0, 2.0, 2.5, 5.0])
        dirichlet = pairwise(Dirichlet, [[0.1, 0.2, 0.7],
                                         [0.5, 0.4, 0.1],
                                         [0.33, 0.33, 0.34],
                                         [0.2, 0.2, 0.4]])
        exponential = pairwise(Exponential, [1.0, 2.5, 5.0, 10.0])
        gamma = pairwise(Gamma, [1.0, 2.5, 1.0, 2.5], [1.5, 1.5, 3.5, 3.5])
        gumbel = pairwise(Gumbel, [-2.0, 4.0, -3.0, 6.0], [1.0, 2.5, 1.0, 2.5])
        laplace = pairwise(Laplace, [-2.0, 4.0, -3.0, 6.0], [1.0, 2.5, 1.0, 2.5])
        lognormal = pairwise(LogNormal, [-2.0, 2.0, -3.0, 3.0], [1.0, 2.0, 1.0, 2.0])
        normal = pairwise(Normal, [-2.0, 2.0, -3.0, 3.0], [1.0, 2.0, 1.0, 2.0])
        onehotcategorical = pairwise(OneHotCategorical, [[0.4, 0.3, 0.3],
                                                         [0.2, 0.7, 0.1],
                                                         [0.33, 0.33, 0.34],
                                                         [0.2, 0.2, 0.6]])
        pareto = pairwise(Pareto, [2.5, 4.0, 2.5, 4.0], [2.25, 3.75, 2.25, 3.75])
        poisson = pairwise(Poisson, [0.3, 1.0, 5.0, 10.0])
        uniform_within_unit = pairwise(Uniform, [0.15, 0.95, 0.2, 0.8], [0.1, 0.9, 0.25, 0.75])
        uniform_positive = pairwise(Uniform, [1, 1.5, 2, 4], [1.2, 2.0, 3, 7])
        uniform_real = pairwise(Uniform, [-2, -1, 0, 2], [-1, 1, 1, 4])
        uniform_pareto = pairwise(Uniform, [6.5, 8.5, 6.5, 8.5], [7.5, 7.5, 9.5, 9.5])

        # These tests should pass with precision = 0.01, but that makes tests very expensive.
        # Instead, we test with precision = 0.1 and only test with higher precision locally
        # when adding a new KL implementation.
        # The following pairs are not tested due to very high variance of the monte carlo
        # estimator; their implementations have been reviewed with extra care:
        # - (pareto, normal)
        self.precision = 0.1  # Set this to 0.01 when testing a new KL implementation.
        self.max_samples = int(1e07)  # Increase this when testing at smaller precision.
        self.samples_per_batch = int(1e04)
        self.finite_examples = [
            (bernoulli, bernoulli),
            (bernoulli, poisson),
            (beta, beta),
            (beta, chi2),
            (beta, exponential),
            (beta, gamma),
            (beta, normal),
            (binomial30, binomial30),
            (categorical, categorical),
            (chi2, chi2),
            (chi2, exponential),
            (chi2, gamma),
            (chi2, normal),
            (dirichlet, dirichlet),
            (exponential, chi2),
            (exponential, exponential),
            (exponential, gamma),
            (exponential, gumbel),
            (exponential, normal),
            (gamma, chi2),
            (gamma, exponential),
            (gamma, gamma),
            (gamma, gumbel),
            (gamma, normal),
            (gumbel, gumbel),
            (gumbel, normal),
            (laplace, laplace),
            (lognormal, lognormal),
            (laplace, normal),
            (normal, gumbel),
            (normal, normal),
            (onehotcategorical, onehotcategorical),
            (pareto, chi2),
            (pareto, pareto),
            (pareto, exponential),
            (pareto, gamma),
            (poisson, poisson),
            (uniform_within_unit, beta),
            (uniform_positive, chi2),
            (uniform_positive, exponential),
            (uniform_positive, gamma),
            (uniform_real, gumbel),
            (uniform_real, normal),
            (uniform_pareto, pareto),
        ]

        self.infinite_examples = [
            (Bernoulli(0), Bernoulli(1)),
            (Bernoulli(1), Bernoulli(0)),
            (Categorical(variable([0.9, 0.1])), Categorical(variable([1, 0]))),
            (Beta(1, 2), Uniform(0.25, 1)),
            (Beta(1, 2), Uniform(0, 0.75)),
            (Beta(1, 2), Uniform(0.25, 0.75)),
            (Beta(1, 2), Pareto(1, 2)),
            (Binomial(31, 0.7), Binomial(30, 0.3)),
            (Chi2(1), Beta(2, 3)),
            (Chi2(1), Pareto(2, 3)),
            (Chi2(1), Uniform(-2, 3)),
            (Exponential(1), Beta(2, 3)),
            (Exponential(1), Pareto(2, 3)),
            (Exponential(1), Uniform(-2, 3)),
            (Gamma(1, 2), Beta(3, 4)),
            (Gamma(1, 2), Pareto(3, 4)),
            (Gamma(1, 2), Uniform(-3, 4)),
            (Gumbel(-1, 2), Beta(3, 4)),
            (Gumbel(-1, 2), Chi2(3)),
            (Gumbel(-1, 2), Exponential(3)),
            (Gumbel(-1, 2), Gamma(3, 4)),
            (Gumbel(-1, 2), Pareto(3, 4)),
            (Gumbel(-1, 2), Uniform(-3, 4)),
            (Laplace(-1, 2), Beta(3, 4)),
            (Laplace(-1, 2), Chi2(3)),
            (Laplace(-1, 2), Exponential(3)),
            (Laplace(-1, 2), Gamma(3, 4)),
            (Laplace(-1, 2), Pareto(3, 4)),
            (Laplace(-1, 2), Uniform(-3, 4)),
            (Normal(-1, 2), Beta(3, 4)),
            (Normal(-1, 2), Chi2(3)),
            (Normal(-1, 2), Exponential(3)),
            (Normal(-1, 2), Gamma(3, 4)),
            (Normal(-1, 2), Pareto(3, 4)),
            (Normal(-1, 2), Uniform(-3, 4)),
            (Pareto(2, 1), Chi2(3)),
            (Pareto(2, 1), Exponential(3)),
            (Pareto(2, 1), Gamma(3, 4)),
            (Pareto(1, 2), Normal(-3, 4)),
            (Pareto(1, 2), Pareto(3, 4)),
            (Poisson(2), Bernoulli(0.5)),
            (Poisson(2.3), Binomial(10, 0.2)),
            (Uniform(-1, 1), Beta(2, 2)),
            (Uniform(0, 2), Beta(3, 4)),
            (Uniform(-1, 2), Beta(3, 4)),
            (Uniform(-1, 2), Chi2(3)),
            (Uniform(-1, 2), Exponential(3)),
            (Uniform(-1, 2), Gamma(3, 4)),
            (Uniform(-1, 2), Pareto(3, 4)),
        ]

    def test_kl_monte_carlo(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for (p, _), (_, q) in self.finite_examples:
            print('Testing KL({}, {}) using Monte Carlo'.format(type(p).__name__, type(q).__name__))
            actual = kl_divergence(p, q)
            numerator = 0
            denominator = 0
            while denominator < self.max_samples:
                x = p.sample(sample_shape=(self.samples_per_batch,))
                numerator += (p.log_prob(x) - q.log_prob(x)).sum(0)
                denominator += x.size(0)
                expected = numerator / denominator
                error = torch.abs(expected - actual) / (1 + expected)
                if error[error == error].max() < self.precision:
                    break
            self.assertLess(error[error == error].max(), self.precision, '\n'.join([
                'Incorrect KL({}, {}).'.format(type(p).__name__, type(q).__name__),
                'Expected ({} Monte Carlo samples): {}'.format(denominator, expected),
                'Actual (analytic): {}'.format(actual),
            ]))

    def test_kl_exponential_family(self):
        for (p, _), (_, q) in self.finite_examples:
            if type(p) == type(q) and issubclass(type(p), ExponentialFamily):
                print('Testing KL({}, {}) using Bregman Divergence'.format(type(p).__name__, type(q).__name__))
                actual = kl_divergence(p, q)
                expected = _kl_expfamily_expfamily(p, q)
                if isinstance(expected, Variable) and not isinstance(actual, Variable):
                    expected = expected.data
                self.assertEqual(actual, expected, message='\n'.join([
                    'Incorrect KL({}, {}).'.format(type(p).__name__, type(q).__name__),
                    'Expected (using Bregman Divergence) {}'.format(expected),
                    'Actual (analytic) {}'.format(actual),
                    'max error = {}'.format(torch.abs(actual - expected).max())
                ]))

    def test_kl_infinite(self):
        for p, q in self.infinite_examples:
            self.assertTrue((kl_divergence(p, q) == float('inf')).all(),
                            'Incorrect KL({}, {})'.format(type(p).__name__, type(q).__name__))

    def test_kl_edgecases(self):
        self.assertEqual(kl_divergence(Bernoulli(0), Bernoulli(0)), 0)
        self.assertEqual(kl_divergence(Bernoulli(1), Bernoulli(1)), 0)
        self.assertEqual(kl_divergence(Categorical(variable([0, 1])), Categorical(variable([0, 1]))), 0)

    def test_kl_shape(self):
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                try:
                    kl = kl_divergence(dist, dist)
                except NotImplementedError:
                    continue
                expected_shape = dist.batch_shape if dist.batch_shape else torch.Size(SCALAR_SHAPE)
                self.assertEqual(kl.shape, expected_shape, message='\n'.join([
                    '{} example {}/{}'.format(Dist.__name__, i + 1, len(params)),
                    'Expected {}'.format(expected_shape),
                    'Actual {}'.format(kl.shape),
                ]))

    def test_entropy_monte_carlo(self):
        set_rng_seed(0)  # see Note [Randomized statistical tests]
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                try:
                    actual = dist.entropy()
                except NotImplementedError:
                    continue
                x = dist.sample(sample_shape=(50000,))
                expected = -dist.log_prob(x).mean(0)
                if isinstance(actual, Variable):
                    actual = actual.data
                    expected = expected.data
                ignore = (expected == float('inf'))
                expected[ignore] = actual[ignore]
                self.assertEqual(actual, expected, prec=0.2, message='\n'.join([
                    '{} example {}/{}, incorrect .entropy().'.format(Dist.__name__, i + 1, len(params)),
                    'Expected (monte carlo) {}'.format(expected),
                    'Actual (analytic) {}'.format(actual),
                    'max error = {}'.format(torch.abs(actual - expected).max()),
                ]))

    def test_entropy_exponential_family(self):
        for Dist, params in EXAMPLES:
            if not issubclass(Dist, ExponentialFamily):
                continue
            for i, param in enumerate(params):
                dist = Dist(**param)
                try:
                    actual = dist.entropy()
                except NotImplementedError:
                    continue
                try:
                    expected = ExponentialFamily.entropy(dist)
                except NotImplementedError:
                    continue
                if isinstance(expected, Variable) and not isinstance(actual, Variable):
                    expected = expected.data
                self.assertEqual(actual, expected, message='\n'.join([
                    '{} example {}/{}, incorrect .entropy().'.format(Dist.__name__, i + 1, len(params)),
                    'Expected (Bregman Divergence) {}'.format(expected),
                    'Actual (analytic) {}'.format(actual),
                    'max error = {}'.format(torch.abs(actual - expected).max())
                ]))


class TestConstraints(TestCase):
    def test_params_contains(self):
        for Dist, params in EXAMPLES:
            for i, param in enumerate(params):
                dist = Dist(**param)
                for name, value in param.items():
                    if isinstance(value, numbers.Number):
                        value = torch.Tensor([value])
                    if Dist in (Categorical, OneHotCategorical, Multinomial) and name == 'probs':
                        # These distributions accept positive probs, but elsewhere we
                        # use a stricter constraint to the simplex.
                        value = value / value.sum(-1, True)
                    try:
                        constraint = dist.params[name]
                    except KeyError:
                        continue  # ignore optional parameters
                    if is_dependent(constraint):
                        continue
                    message = '{} example {}/{} parameter {} = {}'.format(
                        Dist.__name__, i + 1, len(params), name, value)
                    self.assertTrue(constraint.check(value).all(), msg=message)

    def test_support_contains(self):
        for Dist, params in EXAMPLES:
            self.assertIsInstance(Dist.support, Constraint)
            for i, param in enumerate(params):
                dist = Dist(**param)
                value = dist.sample()
                constraint = dist.support
                message = '{} example {}/{} sample = {}'.format(
                    Dist.__name__, i + 1, len(params), value)
                self.assertTrue(constraint.check(value).all(), msg=message)


class TestNumericalStability(TestCase):
    def _test_pdf_score(self,
                        dist_class,
                        x,
                        expected_value,
                        probs=None,
                        logits=None,
                        expected_gradient=None,
                        prec=1e-5):
        if probs is not None:
            p = Variable(probs, requires_grad=True)
            dist = dist_class(p)
        else:
            p = Variable(logits, requires_grad=True)
            dist = dist_class(logits=p)
        log_pdf = dist.log_prob(Variable(x))
        log_pdf.sum().backward()
        self.assertEqual(log_pdf.data,
                         expected_value,
                         prec=prec,
                         message='Incorrect value for tensor type: {}. Expected = {}, Actual = {}'
                         .format(type(x), expected_value, log_pdf.data))
        if expected_gradient is not None:
            self.assertEqual(p.grad.data,
                             expected_gradient,
                             prec=prec,
                             message='Incorrect gradient for tensor type: {}. Expected = {}, Actual = {}'
                             .format(type(x), expected_gradient, p.grad.data))

    def test_bernoulli_gradient(self):
        for tensor_type in [torch.FloatTensor, torch.DoubleTensor]:
            self._test_pdf_score(dist_class=Bernoulli,
                                 probs=tensor_type([0]),
                                 x=tensor_type([0]),
                                 expected_value=tensor_type([0]),
                                 expected_gradient=tensor_type([0]))

            self._test_pdf_score(dist_class=Bernoulli,
                                 probs=tensor_type([0]),
                                 x=tensor_type([1]),
                                 expected_value=tensor_type([_finfo(tensor_type([])).eps]).log(),
                                 expected_gradient=tensor_type([0]))

            self._test_pdf_score(dist_class=Bernoulli,
                                 probs=tensor_type([1e-4]),
                                 x=tensor_type([1]),
                                 expected_value=tensor_type([math.log(1e-4)]),
                                 expected_gradient=tensor_type([10000]))

            # Lower precision due to:
            # >>> 1 / (1 - torch.FloatTensor([0.9999]))
            # 9998.3408
            # [torch.FloatTensor of size 1]
            self._test_pdf_score(dist_class=Bernoulli,
                                 probs=tensor_type([1 - 1e-4]),
                                 x=tensor_type([0]),
                                 expected_value=tensor_type([math.log(1e-4)]),
                                 expected_gradient=tensor_type([-10000]),
                                 prec=2)

            self._test_pdf_score(dist_class=Bernoulli,
                                 logits=tensor_type([math.log(9999)]),
                                 x=tensor_type([0]),
                                 expected_value=tensor_type([math.log(1e-4)]),
                                 expected_gradient=tensor_type([-1]),
                                 prec=1e-3)

    def test_bernoulli_with_logits_underflow(self):
        for tensor_type, lim in ([(torch.FloatTensor, -1e38),
                                  (torch.DoubleTensor, -1e308)]):
            self._test_pdf_score(dist_class=Bernoulli,
                                 logits=tensor_type([lim]),
                                 x=tensor_type([0]),
                                 expected_value=tensor_type([0]),
                                 expected_gradient=tensor_type([0]))

    def test_bernoulli_with_logits_overflow(self):
        for tensor_type, lim in ([(torch.FloatTensor, 1e38),
                                  (torch.DoubleTensor, 1e308)]):
            self._test_pdf_score(dist_class=Bernoulli,
                                 logits=tensor_type([lim]),
                                 x=tensor_type([1]),
                                 expected_value=tensor_type([0]),
                                 expected_gradient=tensor_type([0]))

    def test_categorical_log_prob(self):
        for tensor_type in ([torch.FloatTensor, torch.DoubleTensor]):
            p = Variable(tensor_type([0, 1]), requires_grad=True)
            categorical = OneHotCategorical(p)
            log_pdf = categorical.log_prob(Variable(tensor_type([0, 1])))
            self.assertEqual(log_pdf.data[0], 0)

    def test_categorical_log_prob_with_logits(self):
        for tensor_type in ([torch.FloatTensor, torch.DoubleTensor]):
            p = Variable(tensor_type([-float('inf'), 0]), requires_grad=True)
            categorical = OneHotCategorical(logits=p)
            log_pdf_prob_1 = categorical.log_prob(Variable(tensor_type([0, 1])))
            self.assertEqual(log_pdf_prob_1.data[0], 0)
            log_pdf_prob_0 = categorical.log_prob(Variable(tensor_type([1, 0])))
            self.assertEqual(log_pdf_prob_0.data[0], -float('inf'), allow_inf=True)

    def test_multinomial_log_prob(self):
        for tensor_type in [torch.FloatTensor, torch.DoubleTensor]:
            p = Variable(tensor_type([0, 1]), requires_grad=True)
            s = Variable(tensor_type([0, 10]))
            multinomial = Multinomial(10, p)
            log_pdf = multinomial.log_prob(s)
            self.assertEqual(log_pdf.data[0], 0)

    def test_multinomial_log_prob_with_logits(self):
        for tensor_type in [torch.FloatTensor, torch.DoubleTensor]:
            p = Variable(tensor_type([-float('inf'), 0]), requires_grad=True)
            multinomial = Multinomial(10, logits=p)
            log_pdf_prob_1 = multinomial.log_prob(Variable(tensor_type([0, 10])))
            self.assertEqual(log_pdf_prob_1.data[0], 0)
            log_pdf_prob_0 = multinomial.log_prob(Variable(tensor_type([10, 0])))
            self.assertEqual(log_pdf_prob_0.data[0], -float('inf'), allow_inf=True)


class TestLazyLogitsInitialization(TestCase):
    def setUp(self):
        self.examples = [e for e in EXAMPLES if e.Dist in
                         (Categorical, OneHotCategorical, Bernoulli, Binomial, Multinomial)]

    def test_lazy_logits_initialization(self):
        for Dist, params in self.examples:
            param = params[0]
            if 'probs' in param:
                probs = param.pop('probs')
                param['logits'] = probs_to_logits(probs)
                dist = Dist(**param)
                shape = (1,) if not dist.event_shape else dist.event_shape
                dist.log_prob(Variable(torch.ones(shape)))
                message = 'Failed for {} example 0/{}'.format(Dist.__name__, len(params))
                self.assertFalse('probs' in vars(dist), msg=message)
                try:
                    dist.enumerate_support()
                except NotImplementedError:
                    pass
                self.assertFalse('probs' in vars(dist), msg=message)
                batch_shape, event_shape = dist.batch_shape, dist.event_shape
                self.assertFalse('probs' in vars(dist), msg=message)

    def test_lazy_probs_initialization(self):
        for Dist, params in self.examples:
            param = params[0]
            if 'probs' in param:
                dist = Dist(**param)
                dist.sample()
                message = 'Failed for {} example 0/{}'.format(Dist.__name__, len(params))
                self.assertFalse('logits' in vars(dist), msg=message)
                try:
                    dist.enumerate_support()
                except NotImplementedError:
                    pass
                self.assertFalse('logits' in vars(dist), msg=message)
                batch_shape, event_shape = dist.batch_shape, dist.event_shape
                self.assertFalse('logits' in vars(dist), msg=message)


@unittest.skipIf(not TEST_NUMPY, "NumPy not found")
class TestAgainstScipy(TestCase):
    def setUp(self):
        positive_var = Variable(torch.Tensor(20,).normal_()).exp()
        positive_var2 = Variable(torch.Tensor(20,).normal_()).exp()
        random_var = Variable(torch.Tensor(20,).normal_())
        random_tensor = torch.Tensor(20,).normal_()
        simplex_tensor = softmax(random_tensor)
        self.distribution_pairs = [
            (
                Bernoulli(simplex_tensor),
                scipy.stats.bernoulli(simplex_tensor)
            ),
            (
                Beta(positive_var, positive_var2),
                scipy.stats.beta(positive_var, positive_var2)
            ),
            (
                Binomial(10, simplex_tensor),
                scipy.stats.binom(10 * np.ones(simplex_tensor.shape), simplex_tensor)
            ),
            (
                Cauchy(random_var, positive_var),
                scipy.stats.cauchy(loc=random_var, scale=positive_var)
            ),
            (
                Dirichlet(positive_var),
                scipy.stats.dirichlet(positive_var)
            ),
            (
                Exponential(positive_var),
                scipy.stats.expon(scale=positive_var.reciprocal())
            ),
            (
                FisherSnedecor(positive_var, 4 + positive_var2),  # var for df2<=4 is undefined
                scipy.stats.f(positive_var, 4 + positive_var2)
            ),
            (
                Gamma(positive_var, positive_var2),
                scipy.stats.gamma(positive_var, scale=positive_var2.reciprocal())
            ),
            (
                Geometric(simplex_tensor),
                scipy.stats.geom(simplex_tensor, loc=-1)
            ),
            (
                Gumbel(random_var, positive_var2),
                scipy.stats.gumbel_r(random_var, positive_var2)
            ),
            (
                Laplace(random_var, positive_var2),
                scipy.stats.laplace(random_var, positive_var2)
            ),
            (
                # Tests fail 1e-5 threshold if scale > 3
                LogNormal(random_var, positive_var.clamp(max=3)),
                scipy.stats.lognorm(s=positive_var.clamp(max=3), scale=random_var.exp())
            ),
            (
                Multinomial(10, simplex_tensor),
                scipy.stats.multinomial(10, simplex_tensor)
            ),
            (
                Normal(random_var, positive_var2),
                scipy.stats.norm(random_var, positive_var2)
            ),
            (
                OneHotCategorical(simplex_tensor),
                scipy.stats.multinomial(1, simplex_tensor)
            ),
            (
                Pareto(positive_var, 2 + positive_var2),
                scipy.stats.pareto(2 + positive_var2, scale=positive_var)
            ),
            (
                Poisson(positive_var),
                scipy.stats.poisson(positive_var)
            ),
            (
                StudentT(2 + positive_var, random_var, positive_var2),
                scipy.stats.t(2 + positive_var, random_var, positive_var2)
            ),
            (
                Uniform(random_var, random_var + positive_var),
                scipy.stats.uniform(random_var, positive_var)
            )
        ]

    def test_mean(self):
        for pytorch_dist, scipy_dist in self.distribution_pairs:
            if isinstance(pytorch_dist, Cauchy):  # Cauchy distribution's mean is nan, skipping check
                continue
            self.assertEqual(pytorch_dist.mean, scipy_dist.mean(), allow_inf=True, message=pytorch_dist)

    def test_variance_stddev(self):
        for pytorch_dist, scipy_dist in self.distribution_pairs:
            if isinstance(pytorch_dist, Cauchy):  # Cauchy distribution's standard deviation is nan, skipping check
                continue
            if isinstance(pytorch_dist, (Multinomial, OneHotCategorical)):
                self.assertEqual(pytorch_dist.variance, np.diag(scipy_dist.cov()), message=pytorch_dist)
                self.assertEqual(pytorch_dist.stddev, np.diag(scipy_dist.cov()) ** 0.5, message=pytorch_dist)
            else:
                self.assertEqual(pytorch_dist.variance, scipy_dist.var(), allow_inf=True, message=pytorch_dist)
                self.assertEqual(pytorch_dist.stddev, scipy_dist.var() ** 0.5, message=pytorch_dist)

    def test_cdf(self):
        for pytorch_dist, scipy_dist in self.distribution_pairs:
            samples = pytorch_dist.sample((5,))
            try:
                cdf = pytorch_dist.cdf(samples)
            except NotImplementedError:
                continue
            self.assertEqual(cdf, scipy_dist.cdf(samples), message=pytorch_dist)

    def test_icdf(self):
        for pytorch_dist, scipy_dist in self.distribution_pairs:
            samples = Variable(torch.rand((5,) + pytorch_dist.batch_shape))
            try:
                icdf = pytorch_dist.icdf(samples)
            except NotImplementedError:
                continue
            self.assertEqual(icdf, scipy_dist.ppf(samples), message=pytorch_dist)


class TestTransforms(TestCase):
    def setUp(self):
        self.transforms = []
        transforms_by_cache_size = {}
        for cache_size in [0, 1]:
            transforms = [
                AbsTransform(cache_size=cache_size),
                ExpTransform(cache_size=cache_size),
                SigmoidTransform(cache_size=cache_size),
                AffineTransform(Variable(torch.Tensor(5).normal_()),
                                Variable(torch.Tensor(5).normal_()),
                                cache_size=cache_size),
                AffineTransform(Variable(torch.Tensor(4, 5).normal_()),
                                Variable(torch.Tensor(4, 5).normal_()),
                                cache_size=cache_size),
                BoltzmannTransform(cache_size=cache_size),
                StickBreakingTransform(cache_size=cache_size),
                LowerCholeskyTransform(cache_size=cache_size),
                ComposeTransform([
                    AffineTransform(Variable(torch.Tensor(4, 5).normal_()),
                                    Variable(torch.Tensor(4, 5).normal_()),
                                    cache_size=cache_size),
                ]),
                ComposeTransform([
                    AffineTransform(Variable(torch.Tensor(4, 5).normal_()),
                                    Variable(torch.Tensor(4, 5).normal_()),
                                    cache_size=cache_size),
                    ExpTransform(cache_size=cache_size),
                ]),
            ]
            for t in transforms[:]:
                transforms.append(t.inv)
            transforms.append(identity_transform)
            self.transforms += transforms
            if cache_size == 0:
                self.unique_transforms = transforms[:]

    def _generate_data(self, transform):
        domain = transform.domain
        codomain = transform.codomain
        x = torch.Tensor(4, 5)
        if domain is constraints.lower_cholesky or codomain is constraints.lower_cholesky:
            x = torch.Tensor(6, 6)
            x = x.normal_()
            return x
        elif domain is constraints.real:
            return x.normal_()
        elif domain is constraints.positive:
            return x.normal_().exp()
        elif domain is constraints.unit_interval:
            return x.uniform_()
        elif domain is constraints.simplex:
            x = x.normal_().exp()
            x /= x.sum(-1, True)
            return x
        raise ValueError('Unsupported domain: {}'.format(domain))

    def test_inv_inv(self):
        for t in self.transforms:
            self.assertTrue(t.inv.inv is t)

    def test_equality(self):
        transforms = self.unique_transforms
        for x, y in product(transforms, transforms):
            if x is y:
                self.assertTrue(x == y)
                self.assertFalse(x != y)
            else:
                self.assertFalse(x == y)
                self.assertTrue(x != y)

        self.assertTrue(identity_transform == identity_transform.inv)
        self.assertFalse(identity_transform != identity_transform.inv)

    def test_forward_inverse_cache(self):
        for transform in self.transforms:
            x = Variable(self._generate_data(transform), requires_grad=True)
            try:
                y = transform(x)
            except NotImplementedError:
                continue
            x2 = transform.inv(y)  # should be implemented at least by caching
            y2 = transform(x2)  # should be implemented at least by caching
            if transform.bijective:
                # verify function inverse
                self.assertEqual(x2, x, message='\n'.join([
                    '{} t.inv(t(-)) error'.format(transform),
                    'x = {}'.format(x),
                    'y = t(x) = {}'.format(y),
                    'x2 = t.inv(y) = {}'.format(x2),
                ]))
            else:
                # verify weaker function pseudo-inverse
                self.assertEqual(y2, y, message='\n'.join([
                    '{} t(t.inv(t(-))) error'.format(transform),
                    'x = {}'.format(x),
                    'y = t(x) = {}'.format(y),
                    'x2 = t.inv(y) = {}'.format(x2),
                    'y2 = t(x2) = {}'.format(y2),
                ]))

    def test_forward_inverse_no_cache(self):
        for transform in self.transforms:
            x = Variable(self._generate_data(transform), requires_grad=True)
            try:
                y = transform(x)
                x2 = transform.inv(y.clone())  # bypass cache
                y2 = transform(x2)
            except NotImplementedError:
                continue
            if transform.bijective:
                # verify function inverse
                self.assertEqual(x2, x, message='\n'.join([
                    '{} t.inv(t(-)) error'.format(transform),
                    'x = {}'.format(x),
                    'y = t(x) = {}'.format(y),
                    'x2 = t.inv(y) = {}'.format(x2),
                ]))
            else:
                # verify weaker function pseudo-inverse
                self.assertEqual(y2, y, message='\n'.join([
                    '{} t(t.inv(t(-))) error'.format(transform),
                    'x = {}'.format(x),
                    'y = t(x) = {}'.format(y),
                    'x2 = t.inv(y) = {}'.format(x2),
                    'y2 = t(x2) = {}'.format(y2),
                ]))

    def test_univariate_forward_jacobian(self):
        for transform in self.transforms:
            x = Variable(self._generate_data(transform), requires_grad=True)
            try:
                y = transform(x)
                actual = transform.log_abs_det_jacobian(x, y)
            except NotImplementedError:
                continue
            expected = torch.abs(grad([y.sum()], [x])[0]).log()
            self.assertEqual(actual, expected, message='\n'.join([
                'Bad {}.log_abs_det_jacobian() disagrees with ()'.format(transform),
                'Expected: {}'.format(expected),
                'Actual: {}'.format(actual),
            ]))

    def test_univariate_inverse_jacobian(self):
        for transform in self.transforms:
            y = Variable(self._generate_data(transform.inv), requires_grad=True)
            try:
                x = transform.inv(y)
                actual = transform.log_abs_det_jacobian(x, y)
            except NotImplementedError:
                continue
            expected = -torch.abs(grad([x.sum()], [y])[0]).log()
            self.assertEqual(actual, expected, message='\n'.join([
                '{}.log_abs_det_jacobian() disagrees with .inv()'.format(transform),
                'Expected: {}'.format(expected),
                'Actual: {}'.format(actual),
            ]))

    def test_transform_shapes(self):
        transform0 = ExpTransform()
        transform1 = BoltzmannTransform()
        transform2 = LowerCholeskyTransform()

        self.assertEqual(transform0.event_dim, 0)
        self.assertEqual(transform1.event_dim, 1)
        self.assertEqual(transform2.event_dim, 2)
        self.assertEqual(ComposeTransform([transform0, transform1]).event_dim, 1)
        self.assertEqual(ComposeTransform([transform0, transform2]).event_dim, 2)
        self.assertEqual(ComposeTransform([transform1, transform2]).event_dim, 2)

    def test_transformed_distribution_shapes(self):
        transform0 = ExpTransform()
        transform1 = BoltzmannTransform()
        transform2 = LowerCholeskyTransform()
        base_dist0 = Normal(Variable(torch.zeros(4, 4)), Variable(torch.ones(4, 4)))
        base_dist1 = Dirichlet(Variable(torch.ones(4, 4)))
        examples = [
            ((4, 4), (), base_dist0),
            ((4,), (4,), base_dist1),
            ((4, 4), (), TransformedDistribution(base_dist0, [transform0])),
            ((4,), (4,), TransformedDistribution(base_dist0, [transform1])),
            ((4,), (4,), TransformedDistribution(base_dist0, [transform0, transform1])),
            ((), (4, 4), TransformedDistribution(base_dist0, [transform0, transform2])),
            ((4,), (4,), TransformedDistribution(base_dist0, [transform1, transform0])),
            ((), (4, 4), TransformedDistribution(base_dist0, [transform1, transform2])),
            ((), (4, 4), TransformedDistribution(base_dist0, [transform2, transform0])),
            ((), (4, 4), TransformedDistribution(base_dist0, [transform2, transform1])),
            ((4,), (4,), TransformedDistribution(base_dist1, [transform0])),
            ((4,), (4,), TransformedDistribution(base_dist1, [transform1])),
            ((), (4, 4), TransformedDistribution(base_dist1, [transform2])),
            ((4,), (4,), TransformedDistribution(base_dist1, [transform0, transform1])),
            ((), (4, 4), TransformedDistribution(base_dist1, [transform0, transform2])),
            ((4,), (4,), TransformedDistribution(base_dist1, [transform1, transform0])),
            ((), (4, 4), TransformedDistribution(base_dist1, [transform1, transform2])),
            ((), (4, 4), TransformedDistribution(base_dist1, [transform2, transform0])),
            ((), (4, 4), TransformedDistribution(base_dist1, [transform2, transform1])),
        ]
        for batch_shape, event_shape, dist in examples:
            self.assertEqual(dist.batch_shape, batch_shape)
            self.assertEqual(dist.event_shape, event_shape)
            x = dist.rsample()
            try:
                dist.log_prob(x)  # this should not crash
            except NotImplementedError:
                continue


class TestConstraintRegistry(TestCase):
    def setUp(self):
        self.constraints = [
            constraints.real,
            constraints.positive,
            constraints.greater_than(variable([-10, -2, 0, 2, 10])),
            constraints.less_than(variable([-10, -2, 0, 2, 10])),
            constraints.unit_interval,
            constraints.interval(variable([-4, -2, 0, 2, 4]),
                                 variable([-3, 3, 1, 5, 5])),
            constraints.simplex,
            constraints.lower_cholesky,
        ]

    def test_biject_to(self):
        for constraint in self.constraints:
            try:
                t = biject_to(constraint)
            except NotImplementedError:
                continue
            self.assertTrue(t.bijective, "biject_to({}) is not bijective".format(constraint))
            x = Variable(torch.Tensor(5, 5)).normal_()
            y = t(x)
            self.assertTrue(constraint.check(y).all(), '\n'.join([
                "Failed to biject_to({})".format(constraint),
                "x = {}".format(x),
                "biject_to(...)(x) = {}".format(y),
            ]))
            x2 = t.inv(y)
            self.assertEqual(x, x2, message="Error in biject_to({}) inverse".format(constraint))

    def test_transform_to(self):
        for constraint in self.constraints:
            t = transform_to(constraint)
            x = Variable(torch.Tensor(5, 5)).normal_()
            y = t(x)
            self.assertTrue(constraint.check(y).all(), "Failed to transform_to({})".format(constraint))
            x2 = t.inv(y)
            y2 = t(x2)
            self.assertEqual(y, y2, message="Error in transform_to({}) pseudoinverse".format(constraint))


if __name__ == '__main__':
    run_tests()
