from typing import Callable, Union, Optional, Tuple, List

import numpy as np
import scipy.integrate as si
import scipy.special as ss
from scipy.fftpack import dct

import hankl
import hankel
import pyfftlog

from cora.util import bilinearmap

from .lssutil import FloatArrayLike


def richardson(
    estimates: List[FloatArrayLike],
    t: float,
    base_pow: int = 1,
    return_table: bool = False,
) -> Union[FloatArrayLike, List[List[FloatArrayLike]]]:
    """Use Richardson extrapolation to improve the accuracy of a sequence of estimates.

    Parameters
    ----------
    estimates
        A list of the estimates. Each successive entry should have an accuracy parameter
        `h` (i.e. step size) which decreases by a factor `t`.
    t
        The rate at which the step size decreases, e.g. t = 2 corresponds to halving the
        step size for each successive entry.
    base_pow
        The powers in the error series. By default this is 1 which will cancel all error
        terms up to and including `k - 1` (where `k` is the length of `estimates`. If
        the series only includes even error terms, this can be set to 2, which will
        cancel up to `2 (k - 1)`.
    return_table
        If True return the full Richardson table, if False just return the highest
        accuracy entry.

    Returns
    -------
    ret
        The highest accuracy estimate after extrapolation if `table` is False, otherwise
        the full triangular extrapolation table.
    """

    k = len(estimates)

    table = []

    for row_ind in range(k):

        newrow = [estimates[row_ind]]

        # Cancel off each successive power at this step size using previous estimates
        for col_ind in range(1, row_ind + 1):

            n = col_ind * base_pow
            r = (t ** n * newrow[col_ind - 1] - table[row_ind - 1][col_ind - 1]) / (
                t ** n - 1.0
            )

            newrow.append(r)

        table.append(newrow)

    return table if return_table else table[k - 1][k - 1]


def _corr_direct(psfunc, log_k0, log_k1, r, k=16):
    # Integrate a power spectrum with direct numerical integration (between k0 and k1)
    # to calculate a correlation function at r.

    # Get the grid of k we will integrate over
    ka = np.logspace(log_k0, log_k1, (1 << k) + 1)[np.newaxis, :]
    ra = r[:, np.newaxis]

    dlk = np.log(ka[0, 1] / ka[0, 0])

    integrand = psfunc(ka) * ka ** 3 / (2 * np.pi ** 2) * np.sinc(ka * ra / np.pi)

    return si.romb(integrand) * dlk


def _corr_fftlog(
    func,
    logrmin,
    logrmax,
    samples_per_decade,
    upsample=1000,
    pad_low=2,
    pad_high=1,
    q_bias=0.0,
):
    # Evaluate a correlation function from a power spectrum using the pyfftlog library.
    # The default values here should mostly work for density and potential power spectra

    # Pad the limits
    rlow = logrmin - pad_low
    rhigh = logrmax + pad_high
    n = samples_per_decade * upsample * (rhigh - rlow)

    dlnr = (rhigh - rlow) * np.log(10.0) / n
    rc = 10 ** ((rhigh + rlow) / 2.0)

    # Initialise and calculate the optimal kr for this n
    kr, xsave = pyfftlog.fhti(n, 0.5, dlnr, q=q_bias, kr=1.0, kropt=1)

    # Get the actual sample locations
    ja = np.arange(n)
    jc = (n + 1) / 2.0
    kc = kr / rc
    ra = rc * np.exp((ja - jc) * dlnr)
    ka = kc * np.exp((ja - jc) * dlnr)
    del ja

    # Perform the transform
    f = func(ka) * ka
    del ka
    ft = pyfftlog.fftl(f, xsave, rk=(kc / rc), tdir=1)

    # Decimate and trim to get to the requested sample density
    rd = ra[::upsample]
    fd = ft[::upsample]
    del ra, ft
    mask = (np.log10(rd) >= logrmin) & (np.log10(rd) <= logrmax)
    rd = rd[mask]
    fd = fd[mask]

    # Apply the final scaling and return
    return rd, fd / rd / (8 * np.pi ** 3) ** 0.5


def _corr_hankel(func, logrmin, logrmax, samples_per_decade, h=1e-5):
    # Perform a hankel integration of density and potential power
    # spectra to get a correlation function. They key here is choosing appropriate values for N and h, and these have
    # been tested against other exact integration approach
    ft = hankel.SymmetricFourierTransform(ndim=3, N=(np.pi / h), h=h)

    r = np.logspace(logrmin, logrmax, int((logrmax - logrmin) * samples_per_decade) + 1)

    # Perform the transform
    F = ft.transform(func, r, ret_err=False) / (2 * np.pi) ** 3

    return r, F


def _corr_hankl_richardson(
    func, logrmin, logrmax, samples_per_decade, richardson_n=6, pad_low=2, pad_high=1
):
    # Evaluate a correlation function from a power spectrum using the hankl library with
    # Richardson extrapolation.
    # The default values here should mostly work for density and potential power spectra

    # Pad the limits
    rlow = logrmin - pad_low
    rhigh = logrmax + pad_high
    n = int(samples_per_decade * (rhigh - rlow))

    # Calculate the correlation function upsampling by a factor 2**ii
    def _work(ii):

        u = 2 ** ii
        k = np.logspace(-rhigh, -rlow, n * u, endpoint=False)

        r, xi = hankl.P2xi(k, func(k), 0, lowring=False)

        rt = r[(u - 1) :: u]
        xit = xi.real[(u - 1) :: u]

        return rt, xit

    rs, estimates = zip(*[_work(ii) for ii in range(richardson_n)])

    # Double check that the samples have all ended up at the same r points
    for r in rs[1:]:
        assert np.allclose(r, rs[0])

    # Trim to the requested set of r values
    mask = (np.log10(rs[0]) >= logrmin) & (np.log10(rs[0]) <= logrmax)
    r = rs[0][mask]
    estimates = [e[mask] for e in estimates]

    # Perform and return the Richardson extrapolation
    return r, richardson(estimates, 2.0)


def ps_to_corr(
    psfunc: Callable[[np.ndarray], np.ndarray],
    minlogr: float = -1,
    maxlogr: float = 5,
    switchlogr: float = 2,
    samples_per_decade: int = 100,
    fftlog: bool = True,
    **kwargs,
) -> Tuple[np.ndarray, np.ndarray]:
    """Transform a 3D power spectrum into a correlation function.

    Parameters
    ----------
    psfunc
        The power spectrum function to integrate.
    minlogr, maxlogr
        The minimum and maximum values of r to calculate (given as log_10(r h /
        Mpc)).
    switchlogr
        The scale below which to directly integrate. Above this the `hankel` package
        integration is used.
    samples
        Return the samples instead of the interpolation function. Mostly useful for
        debugging.
    log_threshold
        Below this level in the correlation function use a linear interpolation. This
        should be set to be around the level where the correlation function starts to
        switch sign.
    samples_per_decade
        How many samples per decade to calculate.
    fftlog
        If set, use the FFTlog method with Richardson extrapolation for the large-r
        calculations. If False use the hankel package.
    kwargs
        If using the hankel package there is one parameter available: `h`. If using
        fftlog there are four parameters: `upsample`, `pad_low`, `pad_high`, and
        `q_bias`.

    Returns
    -------
    [type]
        [description]
    """

    # Create a grid with 100 points per decade
    rlow = np.logspace(
        minlogr,
        switchlogr,
        int((switchlogr - minlogr) * samples_per_decade),
        endpoint=False,
    )

    # Perform the high-r transform
    if fftlog:
        rhigh, Fhigh = _corr_hankl_richardson(
            psfunc, switchlogr, maxlogr, samples_per_decade, **kwargs
        )
    else:
        rhigh, Fhigh = _corr_hankel(
            psfunc, switchlogr, maxlogr, samples_per_decade, **kwargs
        )

    # Explicitly calculate and insert the zero lag correlation
    # TODO: remove the hardcoded limits
    rlow = np.insert(rlow, 0, 0.0)
    Flow = _corr_direct(psfunc, -5, 3, rlow)

    ra = np.concatenate([rlow, rhigh])
    Fr = np.concatenate([Flow, Fhigh])

    return ra, Fr


def legendre_array(lmax: int, mu: np.ndarray) -> np.ndarray:
    """Calculate the Legendre polynomials up to lmax at give mu.

    Parameters
    ----------
    lmax
        The maximum l to calculate.
    mu
        The values of mu (=cos(theta)) to calculate at.

    Returns
    -------
    P_l
        A 2D array of the polynomials for each l and mu.
    """
    lm = np.zeros((lmax + 1, len(mu)), dtype=np.float64)

    for i, v in enumerate(mu):
        lm[:, i] = ss.lpn(lmax, v)[0]

    return lm


def corr_to_clarray(
    corr: Callable[[np.ndarray], np.ndarray],
    lmax: int,
    xarray: np.ndarray,
    xromb: int = 3,
    xwidth: Optional[float] = None,
    q: int = 2,
):
    """Calculate an array of :math:`C_l(\chi_1, \chi_2)`.

    Parameters
    ----------
    corr
        The real space correlation function.
    c
        The cosmology object.
    lmax
        Maximum l to calculate up to.
    xarray
        Array of comoving distances to calculate at.
    xromb
        The Romberg order for integrating over radial bins. This generates an
        exponentially increasing amount of work, so increase carefully.
    xwidth
        Width of radial bin to integrate over. If None (default),
        calculate from the separation of the first two bins.
    q
        Integration accuracy parameter for the Legendre transform

    Returns
    -------
    clxx
        Array of the :math:`C_l(\chi_1, \chi_2)` values, with l as the first axis.
    """

    # The integration over angle will be performed by Gauss-Legendre integration. Here
    # we calculate the points that it will be evaluated at....
    M = q * lmax
    mu, w, wsum = ss.roots_legendre(M, mu=True)

    xlen = xarray.size
    corr_array = np.zeros((M, xlen, xlen))

    # If xromb > 0 we need to integrate over the radial bin width, start by modifying
    # the array of distances to add extra points over which we'll integrate
    if xromb > 0:
        # Calculate the half bin width
        # TODO: make this more accurate for non-uniform bin spacings
        xsort = np.sort(xarray)
        xhalf = np.abs(xsort[1] - xsort[0]) / 2.0 if xwidth is None else xwidth / 2.0
        xint = 2 ** xromb + 1
        xspace = 2.0 * xhalf / 2 ** xromb

        # Calculate the extended z-array with the extra intervals to integrate over
        xa = (
            xarray[:, np.newaxis] + np.linspace(-xhalf, xhalf, xint)[np.newaxis, :]
        ).flatten()
    else:
        xa = xarray

    x1 = xa[np.newaxis, :, np.newaxis]
    x2 = xa[np.newaxis, np.newaxis, :]

    # Split the set of theta values to fill into groups of length ~50 and process each,
    # this is helps reduce memory usage which otherwise we be massively inflated by the
    # extra points we integrate over
    for msec in np.array_split(np.arange(M), M // 50):

        ms = mu[msec][:, np.newaxis, np.newaxis]

        # This a the cosine rule but rewritten in a numerically stable form
        rc = ((x1 - x2) ** 2 + 2 * x1 * x2 * (1 - ms)) ** 0.5
        corr1 = corr(rc)

        # If zromb then we need to integrate over the redshift bins which we do by
        # fixed order romberg
        if xromb > 0:
            corr1 = corr1.reshape(-1, xlen, xint, xlen, xint)
            corr1 = si.romb(corr1, dx=xspace, axis=4)
            corr1 = si.romb(corr1, dx=xspace, axis=2)
            corr1 /= (2 * xhalf) ** 2  # Normalise

        corr_array[msec, :, :] = corr1

    lm = legendre_array(lmax, mu)
    lm *= w * 4.0 * np.pi / wsum

    clxx = np.dot(lm, corr_array.reshape(M, -1))
    clxx = clxx.reshape(lmax + 1, xlen, xlen)

    return clxx


def ps_to_aps_flat(
    psfunc: Callable[[np.ndarray], np.ndarray],
    n_k: int = 0,
    n_mu: int = 0,
) -> Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]:
    """Calculate a multi-distance angular power spectrum from a 3D power spectrum.

    This takes a flat sky limit.

    Parameters
    ----------
    psfunc
        The power spectrum function.
    c
        The cosmology object.
    n_k
        Multiply the power spectrum by extra powers of k.
    n_mu
        Multiply by powers of mu, the angle of the Fourier mode to the line of sight.

    Returns
    -------
    aps
        A function which can calculate the power spectrum :math:`C_l(\chi_1, \chi_2)`
    """
    kperpmin = 1e-4
    kperpmax = 40.0
    nkperp = 500
    kparmax = 20.0
    nkpar = 32768

    kperp = np.logspace(np.log10(kperpmin), np.log10(kperpmax), nkperp)[:, np.newaxis]
    kpar = np.linspace(0, kparmax, nkpar)[np.newaxis, :]

    k = (kpar ** 2 + kperp ** 2) ** 0.5
    mu = kpar / k

    # Calculate the power spectrum on a 2D grid and FFT the line of sight axis to turn
    # it into something like a separation
    dd = psfunc(k) * k ** n_k * mu ** n_mu
    aps_dd = dct(dd, type=1) * kparmax / (2 * nkpar)

    def _interp2d(arr, x, y):
        # Interpolate a 2D array, broadcasting as needed
        x, y = np.broadcast_arrays(x, y)
        sh = x.shape

        x, y = x.flatten(), y.flatten()
        v = np.zeros_like(x)
        bilinearmap.interp(arr, x, y, v)

        return v.reshape(sh)

    def _aps(la, xa1, xa2):
        # A closure that will calculate the angular power spectrum
        xc = 0.5 * (xa1 + xa2)
        rpar = np.abs(xa2 - xa1)

        # Bump anything that is zero upwards to avoid a log zero warning.
        la = np.where(la == 0.0, 1e-10, la)

        x = (
            (np.log10(la) - np.log10(xc * kperpmin))
            / np.log10(kperpmax / kperpmin)
            * (nkperp - 1)
        )
        y = rpar / (np.pi / kparmax)
        clzz = _interp2d(aps_dd, x, y) / (xc ** 2 * np.pi)

        return clzz

    return _aps
