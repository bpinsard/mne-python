"""A module which implements the time frequency estimation.

Authors : Alexandre Gramfort, alexandre.gramfort@telecom-paristech.fr
          Hari Bharadwaj <hari@nmr.mgh.harvard.edu>

License : BSD 3-clause

Morlet code inspired by Matlab code from Sheraz Khan & Brainstorm & SPM
"""

import warnings
from math import sqrt
from copy import deepcopy
import numpy as np
from scipy import linalg
from scipy.fftpack import fftn, ifftn

from ..fixes import partial
from ..baseline import rescale
from ..parallel import parallel_func
from ..utils import logger, verbose, requires_h5py
from ..channels.channels import ContainsMixin, PickDropChannelsMixin
from ..io.pick import pick_info, pick_types
from ..utils import check_fname
from .multitaper import dpss_windows
from .._hdf5 import write_hdf5, read_hdf5


def _get_data(inst, return_itc):
    """Get data from Epochs or Evoked instance as epochs x ch x time"""
    from ..epochs import Epochs
    from ..evoked import Evoked
    if not isinstance(inst, (Epochs, Evoked)):
        raise TypeError('inst must be Epochs or Evoked')
    if isinstance(inst, Epochs):
        data = inst.get_data()
    else:
        if return_itc:
            raise ValueError('return_itc must be False for evoked data')
        data = inst.data[np.newaxis, ...].copy()
    return data


def morlet(sfreq, freqs, n_cycles=7, sigma=None, zero_mean=False, Fs=None):
    """Compute Wavelets for the given frequency range

    Parameters
    ----------
    sfreq : float
        Sampling Frequency
    freqs : array
        frequency range of interest (1 x Frequencies)
    n_cycles: float | array of float
        Number of cycles. Fixed number or one per frequency.
    sigma : float, (optional)
        It controls the width of the wavelet ie its temporal
        resolution. If sigma is None the temporal resolution
        is adapted with the frequency like for all wavelet transform.
        The higher the frequency the shorter is the wavelet.
        If sigma is fixed the temporal resolution is fixed
        like for the short time Fourier transform and the number
        of oscillations increases with the frequency.
    zero_mean : bool
        Make sure the wavelet is zero mean

    Returns
    -------
    Ws : list of array
        Wavelets time series
    """
    Ws = list()
    n_cycles = np.atleast_1d(n_cycles)

    # deprecate Fs
    if Fs is not None:
        sfreq = Fs
        warnings.warn("`Fs` is deprecated and will be removed in v0.10. "
                      "Use `sfreq` instead", DeprecationWarning)

    if (n_cycles.size != 1) and (n_cycles.size != len(freqs)):
        raise ValueError("n_cycles should be fixed or defined for "
                         "each frequency.")
    for k, f in enumerate(freqs):
        if len(n_cycles) != 1:
            this_n_cycles = n_cycles[k]
        else:
            this_n_cycles = n_cycles[0]
        # fixed or scale-dependent window
        if sigma is None:
            sigma_t = this_n_cycles / (2.0 * np.pi * f)
        else:
            sigma_t = this_n_cycles / (2.0 * np.pi * sigma)
        # this scaling factor is proportional to (Tallon-Baudry 98):
        # (sigma_t*sqrt(pi))^(-1/2);
        t = np.arange(0., 5. * sigma_t, 1.0 / sfreq)
        t = np.r_[-t[::-1], t[1:]]
        oscillation = np.exp(2.0 * 1j * np.pi * f * t)
        gaussian_enveloppe = np.exp(-t ** 2 / (2.0 * sigma_t ** 2))
        if zero_mean:  # to make it zero mean
            real_offset = np.exp(- 2 * (np.pi * f * sigma_t) ** 2)
            oscillation -= real_offset
        W = oscillation * gaussian_enveloppe
        W /= sqrt(0.5) * linalg.norm(W.ravel())
        Ws.append(W)
    return Ws


def _dpss_wavelet(sfreq, freqs, n_cycles=7, time_bandwidth=4.0,
                  zero_mean=False):
    """Compute Wavelets for the given frequency range

    Parameters
    ----------
    sfreq : float
        Sampling Frequency.
    freqs : ndarray, shape (n_freqs,)
        The frequencies in Hz.
    n_cycles : float | ndarray, shape (n_freqs,)
        The number of cycles globally or for each frequency.
        Defaults to 7.
    time_bandwidth : float, (optional)
        Time x Bandwidth product.
        The number of good tapers (low-bias) is chosen automatically based on
        this to equal floor(time_bandwidth - 1).
        Default is 4.0, giving 3 good tapers.

    Returns
    -------
    Ws : list of array
        Wavelets time series
    """
    Ws = list()
    if time_bandwidth < 2.0:
        raise ValueError("time_bandwidth should be >= 2.0 for good tapers")
    n_taps = int(np.floor(time_bandwidth - 1))
    n_cycles = np.atleast_1d(n_cycles)

    if n_cycles.size != 1 and n_cycles.size != len(freqs):
        raise ValueError("n_cycles should be fixed or defined for "
                         "each frequency.")

    for m in range(n_taps):
        Wm = list()
        for k, f in enumerate(freqs):
            if len(n_cycles) != 1:
                this_n_cycles = n_cycles[k]
            else:
                this_n_cycles = n_cycles[0]

            t_win = this_n_cycles / float(f)
            t = np.arange(0., t_win, 1.0 / sfreq)
            # Making sure wavelets are centered before tapering
            oscillation = np.exp(2.0 * 1j * np.pi * f * (t - t_win / 2.))

            # Get dpss tapers
            tapers, conc = dpss_windows(t.shape[0], time_bandwidth / 2.,
                                        n_taps)

            Wk = oscillation * tapers[m]
            if zero_mean:  # to make it zero mean
                real_offset = Wk.mean()
                Wk -= real_offset
            Wk /= sqrt(0.5) * linalg.norm(Wk.ravel())

            Wm.append(Wk)

        Ws.append(Wm)

    return Ws


def _centered(arr, newsize):
    """Aux Function to center data"""
    # Return the center newsize portion of the array.
    newsize = np.asarray(newsize)
    currsize = np.array(arr.shape)
    startind = (currsize - newsize) // 2
    endind = startind + newsize
    myslice = [slice(startind[k], endind[k]) for k in range(len(endind))]
    return arr[tuple(myslice)]


def _cwt_fft(X, Ws, mode="same"):
    """Compute cwt with fft based convolutions
    Return a generator over signals.
    """
    X = np.asarray(X)

    # Precompute wavelets for given frequency range to save time
    n_signals, n_times = X.shape
    n_freqs = len(Ws)

    Ws_max_size = max(W.size for W in Ws)
    size = n_times + Ws_max_size - 1
    # Always use 2**n-sized FFT
    fsize = 2 ** int(np.ceil(np.log2(size)))

    # precompute FFTs of Ws
    fft_Ws = np.empty((n_freqs, fsize), dtype=np.complex128)
    for i, W in enumerate(Ws):
        if len(W) > n_times:
            raise ValueError('Wavelet is too long for such a short signal. '
                             'Reduce the number of cycles.')
        fft_Ws[i] = fftn(W, [fsize])

    for k, x in enumerate(X):
        if mode == "full":
            tfr = np.zeros((n_freqs, fsize), dtype=np.complex128)
        elif mode == "same" or mode == "valid":
            tfr = np.zeros((n_freqs, n_times), dtype=np.complex128)

        fft_x = fftn(x, [fsize])
        for i, W in enumerate(Ws):
            ret = ifftn(fft_x * fft_Ws[i])[:n_times + W.size - 1]
            if mode == "valid":
                sz = abs(W.size - n_times) + 1
                offset = (n_times - sz) / 2
                tfr[i, offset:(offset + sz)] = _centered(ret, sz)
            else:
                tfr[i, :] = _centered(ret, n_times)
        yield tfr


def _cwt_convolve(X, Ws, mode='same'):
    """Compute time freq decomposition with temporal convolutions
    Return a generator over signals.
    """
    X = np.asarray(X)

    n_signals, n_times = X.shape
    n_freqs = len(Ws)

    # Compute convolutions
    for x in X:
        tfr = np.zeros((n_freqs, n_times), dtype=np.complex128)
        for i, W in enumerate(Ws):
            ret = np.convolve(x, W, mode=mode)
            if len(W) > len(x):
                raise ValueError('Wavelet is too long for such a short '
                                 'signal. Reduce the number of cycles.')
            if mode == "valid":
                sz = abs(W.size - n_times) + 1
                offset = (n_times - sz) / 2
                tfr[i, offset:(offset + sz)] = ret
            else:
                tfr[i] = ret
        yield tfr


def cwt_morlet(X, sfreq, freqs, use_fft=True, n_cycles=7.0, zero_mean=False,
               Fs=None):
    """Compute time freq decomposition with Morlet wavelets

    This function operates directly on numpy arrays. Consider using
    `tfr_morlet` to process `Epochs` or `Evoked` instances.

    Parameters
    ----------
    X : array of shape [n_signals, n_times]
        signals (one per line)
    sfreq : float
        sampling Frequency
    freqs : array
        Array of frequencies of interest
    use_fft : bool
        Compute convolution with FFT or temoral convolution.
    n_cycles: float | array of float
        Number of cycles. Fixed number or one per frequency.
    zero_mean : bool
        Make sure the wavelets are zero mean.

    Returns
    -------
    tfr : 3D array
        Time Frequency Decompositions (n_signals x n_frequencies x n_times)
    """
    # deprecate Fs
    if Fs is not None:
        sfreq = Fs
        warnings.warn("`Fs` is deprecated and will be removed in v0.10. "
                      "Use `sfreq` instead", DeprecationWarning)

    mode = 'same'
    # mode = "valid"
    n_signals, n_times = X.shape
    n_frequencies = len(freqs)

    # Precompute wavelets for given frequency range to save time
    Ws = morlet(sfreq, freqs, n_cycles=n_cycles, zero_mean=zero_mean)

    if use_fft:
        coefs = _cwt_fft(X, Ws, mode)
    else:
        coefs = _cwt_convolve(X, Ws, mode)

    tfrs = np.empty((n_signals, n_frequencies, n_times), dtype=np.complex)
    for k, tfr in enumerate(coefs):
        tfrs[k] = tfr

    return tfrs


def cwt(X, Ws, use_fft=True, mode='same', decim=1):
    """Compute time freq decomposition with continuous wavelet transform

    Parameters
    ----------
    X : array of shape [n_signals, n_times]
        signals (one per line)
    Ws : list of array
        Wavelets time series
    use_fft : bool
        Use FFT for convolutions
    mode : 'same' | 'valid' | 'full'
        Convention for convolution
    decim : int
        Temporal decimation factor

    Returns
    -------
    tfr : 3D array
        Time Frequency Decompositions (n_signals x n_frequencies x n_times)
    """
    n_signals, n_times = X[:, ::decim].shape
    n_frequencies = len(Ws)

    if use_fft:
        coefs = _cwt_fft(X, Ws, mode)
    else:
        coefs = _cwt_convolve(X, Ws, mode)

    tfrs = np.empty((n_signals, n_frequencies, n_times), dtype=np.complex)
    for k, tfr in enumerate(coefs):
        tfrs[k] = tfr[..., ::decim]

    return tfrs


def _time_frequency(X, Ws, use_fft, decim):
    """Aux of time_frequency for parallel computing over channels
    """
    n_epochs, n_times = X.shape
    n_times = n_times // decim + bool(n_times % decim)
    n_frequencies = len(Ws)
    psd = np.zeros((n_frequencies, n_times))  # PSD
    plf = np.zeros((n_frequencies, n_times), np.complex)  # phase lock

    mode = 'same'
    if use_fft:
        tfrs = _cwt_fft(X, Ws, mode)
    else:
        tfrs = _cwt_convolve(X, Ws, mode)

    for tfr in tfrs:
        tfr = tfr[:, ::decim]
        tfr_abs = np.abs(tfr)
        psd += tfr_abs ** 2
        plf += tfr / tfr_abs
    psd /= n_epochs
    plf = np.abs(plf) / n_epochs
    return psd, plf


@verbose
def single_trial_power(data, sfreq, frequencies, use_fft=True, n_cycles=7,
                       baseline=None, baseline_mode='ratio', times=None,
                       decim=1, n_jobs=1, zero_mean=False, Fs=None,
                       verbose=None):
    """Compute time-frequency power on single epochs

    Parameters
    ----------
    data : array of shape [n_epochs, n_channels, n_times]
        The epochs
    sfreq : float
        Sampling rate
    frequencies : array-like
        The frequencies
    use_fft : bool
        Use the FFT for convolutions or not.
    n_cycles : float | array of float
        Number of cycles  in the Morlet wavelet. Fixed number
        or one per frequency.
    baseline : None (default) or tuple of length 2
        The time interval to apply baseline correction.
        If None do not apply it. If baseline is (a, b)
        the interval is between "a (s)" and "b (s)".
        If a is None the beginning of the data is used
        and if b is None then b is set to the end of the interval.
        If baseline is equal ot (None, None) all the time
        interval is used.
    baseline_mode : None | 'ratio' | 'zscore'
        Do baseline correction with ratio (power is divided by mean
        power during baseline) or zscore (power is divided by standard
        deviation of power during baseline after subtracting the mean,
        power = [power - mean(power_baseline)] / std(power_baseline))
    times : array
        Required to define baseline
    decim : int
        Temporal decimation factor
    n_jobs : int
        The number of epochs to process at the same time
    zero_mean : bool
        Make sure the wavelets are zero mean.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Returns
    -------
    power : 4D array
        Power estimate (Epochs x Channels x Frequencies x Timepoints).
    """
    # deprecate Fs
    if Fs is not None:
        sfreq = Fs
        warnings.warn("`Fs` is deprecated and will be removed in v0.10. "
                      "Use `sfreq` instead", DeprecationWarning)

    mode = 'same'
    n_frequencies = len(frequencies)
    n_epochs, n_channels, n_times = data[:, :, ::decim].shape

    # Precompute wavelets for given frequency range to save time
    Ws = morlet(sfreq, frequencies, n_cycles=n_cycles, zero_mean=zero_mean)

    parallel, my_cwt, _ = parallel_func(cwt, n_jobs)

    logger.info("Computing time-frequency power on single epochs...")

    power = np.empty((n_epochs, n_channels, n_frequencies, n_times),
                     dtype=np.float)

    # Package arguments for `cwt` here to minimize omissions where only one of
    # the two calls below is updated with new function arguments.
    cwt_kw = dict(Ws=Ws, use_fft=use_fft, mode=mode, decim=decim)
    if n_jobs == 1:
        for k, e in enumerate(data):
            x = cwt(e, **cwt_kw)
            power[k] = (x * x.conj()).real
    else:
        # Precompute tf decompositions in parallel
        tfrs = parallel(my_cwt(e, **cwt_kw) for e in data)
        for k, tfr in enumerate(tfrs):
            power[k] = (tfr * tfr.conj()).real

    # Run baseline correction.  Be sure to decimate the times array as well if
    # needed.
    if times is not None:
        times = times[::decim]
    power = rescale(power, times, baseline, baseline_mode, copy=False)
    return power


def _induced_power_cwt(data, sfreq, frequencies, use_fft=True, n_cycles=7,
                       decim=1, n_jobs=1, zero_mean=False, Fs=None):
    """Compute time induced power and inter-trial phase-locking factor

    The time frequency decomposition is done with Morlet wavelets

    Parameters
    ----------
    data : array
        3D array of shape [n_epochs, n_channels, n_times]
    sfreq : float
        sampling Frequency
    frequencies : array
        Array of frequencies of interest
    use_fft : bool
        Compute transform with fft based convolutions or temporal
        convolutions.
    n_cycles : float | array of float
        Number of cycles. Fixed number or one per frequency.
    decim: int
        Temporal decimation factor
    n_jobs : int
        The number of CPUs used in parallel. All CPUs are used in -1.
        Requires joblib package.
    zero_mean : bool
        Make sure the wavelets are zero mean.

    Returns
    -------
    power : 2D array
        Induced power (Channels x Frequencies x Timepoints).
        Squared amplitude of time-frequency coefficients.
    phase_lock : 2D array
        Phase locking factor in [0, 1] (Channels x Frequencies x Timepoints)
    """
    n_frequencies = len(frequencies)
    n_epochs, n_channels, n_times = data[:, :, ::decim].shape

    # Precompute wavelets for given frequency range to save time
    Ws = morlet(sfreq, frequencies, n_cycles=n_cycles, zero_mean=zero_mean)

    psd = np.empty((n_channels, n_frequencies, n_times))
    plf = np.empty((n_channels, n_frequencies, n_times))
    # Separate to save memory for n_jobs=1
    parallel, my_time_frequency, _ = parallel_func(_time_frequency, n_jobs)
    psd_plf = parallel(my_time_frequency(data[:, c, :], Ws, use_fft, decim)
                       for c in range(n_channels))
    for c, (psd_c, plf_c) in enumerate(psd_plf):
        psd[c, :, :], plf[c, :, :] = psd_c, plf_c
    return psd, plf


def _preproc_tfr(data, times, freqs, tmin, tmax, fmin, fmax, mode,
                 baseline, vmin, vmax, dB):
    """Aux Function to prepare tfr computation"""
    from ..viz.utils import _setup_vmin_vmax

    if mode is not None and baseline is not None:
        logger.info("Applying baseline correction '%s' during %s" %
                    (mode, baseline))
        data = rescale(data.copy(), times, baseline, mode)

    # crop time
    itmin, itmax = None, None
    if tmin is not None:
        itmin = np.where(times >= tmin)[0][0]
    if tmax is not None:
        itmax = np.where(times <= tmax)[0][-1]

    times = times[itmin:itmax]

    # crop freqs
    ifmin, ifmax = None, None
    if fmin is not None:
        ifmin = np.where(freqs >= fmin)[0][0]
    if fmax is not None:
        ifmax = np.where(freqs <= fmax)[0][-1]

    freqs = freqs[ifmin:ifmax]

    # crop data
    data = data[:, ifmin:ifmax, itmin:itmax]

    times *= 1e3
    if dB:
        data = 20 * np.log10(data)

    vmin, vmax = _setup_vmin_vmax(data, vmin, vmax)
    return data, times, freqs, vmin, vmax


class AverageTFR(ContainsMixin, PickDropChannelsMixin):
    """Container for Time-Frequency data

    Can for example store induced power at sensor level or intertrial
    coherence.

    Parameters
    ----------
    info : Info
        The measurement info.
    data : ndarray, shape (n_channels, n_freqs, n_times)
        The data.
    times : ndarray, shape (n_times,)
        The time values in seconds.
    freqs : ndarray, shape (n_freqs,)
        The frequencies in Hz.
    nave : int
        The number of averaged TFRs.
    comment : str | None
        Comment on the data, e.g., the experimental condition.
        Defaults to None.
    method : str | None
        Comment on the method used to compute the data, e.g., morlet wavelet.
        Defaults to None.

    Attributes
    ----------
    ch_names : list
        The names of the channels.
    """
    @verbose
    def __init__(self, info, data, times, freqs, nave, comment=None,
                 method=None, verbose=None):
        self.info = info
        if data.ndim != 3:
            raise ValueError('data should be 3d. Got %d.' % data.ndim)
        n_channels, n_freqs, n_times = data.shape
        if n_channels != len(info['chs']):
            raise ValueError("Number of channels and data size don't match"
                             " (%d != %d)." % (n_channels, len(info['chs'])))
        if n_freqs != len(freqs):
            raise ValueError("Number of frequencies and data size don't match"
                             " (%d != %d)." % (n_freqs, len(freqs)))
        if n_times != len(times):
            raise ValueError("Number of times and data size don't match"
                             " (%d != %d)." % (n_times, len(times)))
        self.data = data
        self.times = times
        self.freqs = freqs
        self.nave = nave
        self.comment = comment
        self.method = method

    @property
    def ch_names(self):
        return self.info['ch_names']

    @verbose
    def plot(self, picks=None, baseline=None, mode='mean', tmin=None,
             tmax=None, fmin=None, fmax=None, vmin=None, vmax=None,
             cmap='RdBu_r', dB=False, colorbar=True, show=True,
             title=None, verbose=None):
        """Plot TFRs in a topography with images

        Parameters
        ----------
        picks : array-like of int | None
            The indices of the channels to plot.
        baseline : None (default) or tuple of length 2
            The time interval to apply baseline correction.
            If None do not apply it. If baseline is (a, b)
            the interval is between "a (s)" and "b (s)".
            If a is None the beginning of the data is used
            and if b is None then b is set to the end of the interval.
            If baseline is equal ot (None, None) all the time
            interval is used.
        mode : None | 'logratio' | 'ratio' | 'zscore' | 'mean' | 'percent'
            Do baseline correction with ratio (power is divided by mean
            power during baseline) or zscore (power is divided by standard
            deviation of power during baseline after subtracting the mean,
            power = [power - mean(power_baseline)] / std(power_baseline)).
            If None no baseline correction is applied.
        tmin : None | float
            The first time instant to display. If None the first time point
            available is used.
        tmax : None | float
            The last time instant to display. If None the last time point
            available is used.
        fmin : None | float
            The first frequency to display. If None the first frequency
            available is used.
        fmax : None | float
            The last frequency to display. If None the last frequency
            available is used.
        vmin : float | None
            The mininum value an the color scale. If vmin is None, the data
            minimum value is used.
        vmax : float | None
            The maxinum value an the color scale. If vmax is None, the data
            maximum value is used.
        cmap : matplotlib colormap | str
            The colormap to use. Defaults to 'RdBu_r'.
        dB : bool
            If True, 20*log10 is applied to the data to get dB.
        colorbar : bool
            If true, colorbar will be added to the plot
        show : bool
            Call pyplot.show() at the end.
        title : str | None
            String for title. Defaults to None (blank/no title).
        verbose : bool, str, int, or None
            If not None, override default verbose level (see mne.verbose).
        """
        from ..viz.topo import _imshow_tfr
        import matplotlib.pyplot as plt
        times, freqs = self.times.copy(), self.freqs.copy()
        data = self.data[picks]

        data, times, freqs, vmin, vmax = \
            _preproc_tfr(data, times, freqs, tmin, tmax, fmin, fmax, mode,
                         baseline, vmin, vmax, dB)

        tmin, tmax = times[0], times[-1]

        for k, p in zip(range(len(data)), picks):
            plt.figure()
            _imshow_tfr(plt, 0, tmin, tmax, vmin, vmax, ylim=None,
                        tfr=data[k: k + 1], freq=freqs, x_label='Time (ms)',
                        y_label='Frequency (Hz)', colorbar=colorbar,
                        picker=False, cmap=cmap, title=title)

        if show:
            plt.show()
        return

    def plot_topo(self, picks=None, baseline=None, mode='mean', tmin=None,
                  tmax=None, fmin=None, fmax=None, vmin=None, vmax=None,
                  layout=None, cmap='RdBu_r', title=None, dB=False,
                  colorbar=True, layout_scale=0.945, show=True,
                  border='none', fig_facecolor='k', font_color='w'):
        """Plot TFRs in a topography with images

        Parameters
        ----------
        picks : array-like of int | None
            The indices of the channels to plot. If None all available
            channels are displayed.
        baseline : None (default) or tuple of length 2
            The time interval to apply baseline correction.
            If None do not apply it. If baseline is (a, b)
            the interval is between "a (s)" and "b (s)".
            If a is None the beginning of the data is used
            and if b is None then b is set to the end of the interval.
            If baseline is equal ot (None, None) all the time
            interval is used.
        mode : None | 'logratio' | 'ratio' | 'zscore' | 'mean' | 'percent'
            Do baseline correction with ratio (power is divided by mean
            power during baseline) or zscore (power is divided by standard
            deviation of power during baseline after subtracting the mean,
            power = [power - mean(power_baseline)] / std(power_baseline)).
            If None no baseline correction is applied.
        tmin : None | float
            The first time instant to display. If None the first time point
            available is used.
        tmax : None | float
            The last time instant to display. If None the last time point
            available is used.
        fmin : None | float
            The first frequency to display. If None the first frequency
            available is used.
        fmax : None | float
            The last frequency to display. If None the last frequency
            available is used.
        vmin : float | None
            The mininum value an the color scale. If vmin is None, the data
            minimum value is used.
        vmax : float | None
            The maxinum value an the color scale. If vmax is None, the data
            maximum value is used.
        layout : Layout | None
            Layout instance specifying sensor positions. If possible, the
            correct layout is inferred from the data.
        cmap : matplotlib colormap | str
            The colormap to use. Defaults to 'RdBu_r'.
        title : str
            Title of the figure.
        dB : bool
            If True, 20*log10 is applied to the data to get dB.
        colorbar : bool
            If true, colorbar will be added to the plot
        layout_scale : float
            Scaling factor for adjusting the relative size of the layout
            on the canvas.
        show : bool
            Call pyplot.show() at the end.
        border : str
            matplotlib borders style to be used for each sensor plot.
        fig_facecolor : str | obj
            The figure face color. Defaults to black.
        font_color: str | obj
            The color of tick labels in the colorbar. Defaults to white.
        """
        from ..viz.topo import _imshow_tfr, _plot_topo
        times = self.times.copy()
        freqs = self.freqs
        data = self.data
        info = self.info

        if picks is not None:
            data = data[picks]
            info = pick_info(info, picks)

        data, times, freqs, vmin, vmax = \
            _preproc_tfr(data, times, freqs, tmin, tmax, fmin, fmax,
                         mode, baseline, vmin, vmax, dB)

        if layout is None:
            from mne import find_layout
            layout = find_layout(self.info)

        imshow = partial(_imshow_tfr, tfr=data, freq=freqs, cmap=cmap)

        fig = _plot_topo(info=info, times=times,
                         show_func=imshow, layout=layout,
                         colorbar=colorbar, vmin=vmin, vmax=vmax, cmap=cmap,
                         layout_scale=layout_scale, title=title, border=border,
                         x_label='Time (ms)', y_label='Frequency (Hz)',
                         fig_facecolor=fig_facecolor,
                         font_color=font_color)

        if show:
            import matplotlib.pyplot as plt
            plt.show()

        return fig

    def _check_compat(self, tfr):
        """checks that self and tfr have the same time-frequency ranges"""
        assert np.all(tfr.times == self.times)
        assert np.all(tfr.freqs == self.freqs)

    def __add__(self, tfr):
        self._check_compat(tfr)
        out = self.copy()
        out.data += tfr.data
        return out

    def __iadd__(self, tfr):
        self._check_compat(tfr)
        self.data += tfr.data
        return self

    def __sub__(self, tfr):
        self._check_compat(tfr)
        out = self.copy()
        out.data -= tfr.data
        return out

    def __isub__(self, tfr):
        self._check_compat(tfr)
        self.data -= tfr.data
        return self

    def copy(self):
        """Return a copy of the instance."""
        return deepcopy(self)

    def __repr__(self):
        s = "time : [%f, %f]" % (self.times[0], self.times[-1])
        s += ", freq : [%f, %f]" % (self.freqs[0], self.freqs[-1])
        s += ", nave : %d" % self.nave
        s += ', channels : %d' % self.data.shape[0]
        return "<AverageTFR  |  %s>" % s

    def apply_baseline(self, baseline, mode='mean'):
        """Baseline correct the data

        Parameters
        ----------
        baseline : tuple or list of length 2
            The time interval to apply rescaling / baseline correction.
            If None do not apply it. If baseline is (a, b)
            the interval is between "a (s)" and "b (s)".
            If a is None the beginning of the data is used
            and if b is None then b is set to the end of the interval.
            If baseline is equal to (None, None) all the time
            interval is used.
        mode : 'logratio' | 'ratio' | 'zscore' | 'mean' | 'percent'
            Do baseline correction with ratio (power is divided by mean
            power during baseline) or z-score (power is divided by standard
            deviation of power during baseline after subtracting the mean,
            power = [power - mean(power_baseline)] / std(power_baseline))
            If None, baseline no correction will be performed.
        """
        self.data = rescale(self.data, self.times, baseline, mode, copy=False)

    def plot_topomap(self, tmin=None, tmax=None, fmin=None, fmax=None,
                     ch_type='mag', baseline=None, mode='mean',
                     layout=None, vmin=None, vmax=None, cmap='RdBu_r',
                     sensors=True, colorbar=True, unit=None, res=64, size=2,
                     format='%1.1e', show_names=False, title=None,
                     axes=None, show=True):
        """Plot topographic maps of time-frequency intervals of TFR data

        Parameters
        ----------
        tmin : None | float
            The first time instant to display. If None the first time point
            available is used.
        tmax : None | float
            The last time instant to display. If None the last time point
            available is used.
        fmin : None | float
            The first frequency to display. If None the first frequency
            available is used.
        fmax : None | float
            The last frequency to display. If None the last frequency
            available is used.
        ch_type : 'mag' | 'grad' | 'planar1' | 'planar2' | 'eeg'
            The channel type to plot. For 'grad', the gradiometers are
            collected in pairs and the RMS for each pair is plotted.
        baseline : tuple or list of length 2
            The time interval to apply rescaling / baseline correction.
            If None do not apply it. If baseline is (a, b)
            the interval is between "a (s)" and "b (s)".
            If a is None the beginning of the data is used
            and if b is None then b is set to the end of the interval.
            If baseline is equal to (None, None) all the time
            interval is used.
        mode : 'logratio' | 'ratio' | 'zscore' | 'mean' | 'percent'
            Do baseline correction with ratio (power is divided by mean
            power during baseline) or z-score (power is divided by standard
            deviation of power during baseline after subtracting the mean,
            power = [power - mean(power_baseline)] / std(power_baseline))
            If None, baseline no correction will be performed.
        layout : None | Layout
            Layout instance specifying sensor positions (does not need to
            be specified for Neuromag data). If possible, the correct layout
            file is inferred from the data; if no appropriate layout file was
            found, the layout is automatically generated from the sensor
            locations.
        vmin : float | callable
            The value specfying the lower bound of the color range.
            If None, and vmax is None, -vmax is used. Else np.min(data).
            If callable, the output equals vmin(data).
        vmax : float | callable
            The value specfying the upper bound of the color range.
            If None, the maximum absolute value is used. If vmin is None,
            but vmax is not, defaults to np.min(data).
            If callable, the output equals vmax(data).
        cmap : matplotlib colormap
            Colormap. For magnetometers and eeg defaults to 'RdBu_r', else
            'Reds'.
        sensors : bool | str
            Add markers for sensor locations to the plot. Accepts matplotlib
            plot format string (e.g., 'r+' for red plusses). If True, a circle
            will be used (via .add_artist). Defaults to True.
        colorbar : bool
            Plot a colorbar.
        unit : str | None
            The unit of the channel type used for colorbar labels.
        res : int
            The resolution of the topomap image (n pixels along each side).
        size : float
            Side length per topomap in inches.
        format : str
            String format for colorbar values.
        show_names : bool | callable
            If True, show channel names on top of the map. If a callable is
            passed, channel names will be formatted using the callable; e.g.,
            to delete the prefix 'MEG ' from all channel names, pass the
            function lambda x: x.replace('MEG ', ''). If `mask` is not None,
            only significant sensors will be shown.
        title : str | None
            Title. If None (default), no title is displayed.
        axes : instance of Axes | None
            The axes to plot to. If None the axes is defined automatically.
        show : bool
            Call pyplot.show() at the end.

        Returns
        -------
        fig : matplotlib.figure.Figure
            The figure containing the topography.
        """
        from ..viz import plot_tfr_topomap
        return plot_tfr_topomap(self, tmin=tmin, tmax=tmax, fmin=fmin,
                                fmax=fmax, ch_type=ch_type, baseline=baseline,
                                mode=mode, layout=layout, vmin=vmin, vmax=vmax,
                                cmap=cmap, sensors=sensors, colorbar=colorbar,
                                unit=unit, res=res, size=size, format=format,
                                show_names=show_names, title=title, axes=axes,
                                show=show)

    @requires_h5py
    def save(self, fname, overwrite=False):
        """Save TFR object to hdf5 file

        Parameters
        ----------
        fname : str
            The file name, which should end with -tfr.h5 .
        overwrite : bool
            If True, overwrite file (if it exists). Defaults to false
        """
        write_tfrs(fname, self, overwrite=overwrite)


def _prepare_write_tfr(tfr, condition):
    """Aux function"""
    return (condition, dict(times=tfr.times, freqs=tfr.freqs,
                            data=tfr.data, info=tfr.info, nave=tfr.nave,
                            comment=tfr.comment, method=tfr.method))


@requires_h5py
def write_tfrs(fname, tfr, overwrite=False):
    """Write a TFR dataset to hdf5.

    Parameters
    ----------
    fname : string
        The file name, which should end with -tfr.h5
    tfr : AverageTFR instance, or list of AverageTFR instances
        The TFR dataset, or list of TFR datasets, to save in one file.
        Note. If .comment is not None, a name will be generated on the fly,
        based on the order in which the TFR objects are passed
    overwrite : bool
        If True, overwrite file (if it exists). Defaults to False.
    """
    out = []
    if not isinstance(tfr, (list, tuple)):
        tfr = [tfr]
    for ii, tfr_ in enumerate(tfr):
        comment = ii if tfr_.comment is None else tfr_.comment
        out.append(_prepare_write_tfr(tfr_, condition=comment))
    write_hdf5(fname, out, overwrite=overwrite)


@requires_h5py
def read_tfrs(fname, condition=None):
    """
    Read TFR datasets from hdf5 file.

    Parameters
    ----------
    fname : string
        The file name, which should end with -tfr.h5 .
    condition : int or str | list of int or str | None
        The condition to load. If None, all conditions will be returned.
        Defaults to None.

    Returns
    -------
    tfrs : list of instances of AverageTFR | instance of AverageTFR
        Depending on `condition` either the TFR object or a list of multiple
        TFR objects.
    """

    check_fname(fname, 'tfr', ('-tfr.h5',))

    logger.info('Reading %s ...' % fname)
    tfr_data = read_hdf5(fname)
    if condition is not None:
        tfr_dict = dict(tfr_data)
        if condition not in tfr_dict:
            keys = ['%s' % k for k in tfr_dict]
            raise ValueError('Cannot find condition ("{0}") in this file. '
                             'I can give you "{1}""'
                             .format(condition, " or ".join(keys)))
        out = AverageTFR(**tfr_dict[condition])
    else:
        out = [AverageTFR(**d) for d in list(zip(*tfr_data))[1]]
    return out


def tfr_morlet(inst, freqs, n_cycles, use_fft=False,
               return_itc=True, decim=1, n_jobs=1):
    """Compute Time-Frequency Representation (TFR) using Morlet wavelets

    Parameters
    ----------
    inst : Epochs | Evoked
        The epochs or evoked object.
    freqs : ndarray, shape (n_freqs,)
        The frequencies in Hz.
    n_cycles : float | ndarray, shape (n_freqs,)
        The number of cycles globally or for each frequency.
    use_fft : bool
        The fft based convolution or not.
    return_itc : bool
        Return intertrial coherence (ITC) as well as averaged power.
        Must be ``False`` for evoked data.
    decim : int
        The decimation factor on the time axis. To reduce memory usage.
    n_jobs : int
        The number of jobs to run in parallel.

    Returns
    -------
    power : AverageTFR
        The averaged power.
    itc : AverageTFR
        The intertrial coherence (ITC). Only returned if return_itc
        is True.
    """
    data = _get_data(inst, return_itc)
    picks = pick_types(inst.info, meg=True, eeg=True)
    info = pick_info(inst.info, picks)
    data = data[:, picks, :]
    power, itc = _induced_power_cwt(data, sfreq=info['sfreq'],
                                    frequencies=freqs,
                                    n_cycles=n_cycles, n_jobs=n_jobs,
                                    use_fft=use_fft, decim=decim,
                                    zero_mean=True)
    times = inst.times[::decim].copy()
    nave = len(data)
    out = AverageTFR(info, power, times, freqs, nave, method='morlet-power')
    if return_itc:
        out = (out, AverageTFR(info, itc, times, freqs, nave,
                               method='morlet-itc'))
    return out


@verbose
def _induced_power_mtm(data, sfreq, frequencies, time_bandwidth=4.0,
                       use_fft=True, n_cycles=7, decim=1, n_jobs=1,
                       zero_mean=True, verbose=None):
    """Compute time induced power and inter-trial phase-locking factor

    The time frequency decomposition is done with DPSS wavelets

    Parameters
    ----------
    data : np.ndarray, shape (n_epochs, n_channels, n_times)
        The input data.
    sfreq : float
        sampling Frequency
    frequencies : np.ndarray, shape (n_frequencies,)
        Array of frequencies of interest
    time_bandwidth : float
        Time x (Full) Bandwidth product.
        The number of good tapers (low-bias) is chosen automatically based on
        this to equal floor(time_bandwidth - 1). Default is 4.0 (3 tapers).
    use_fft : bool
        Compute transform with fft based convolutions or temporal
        convolutions. Defaults to True.
    n_cycles : float | np.ndarray shape (n_frequencies,)
        Number of cycles. Fixed number or one per frequency. Defaults to 7.
    decim: int
        Temporal decimation factor. Defaults to 1.
    n_jobs : int
        The number of CPUs used in parallel. All CPUs are used in -1.
        Requires joblib package. Defaults to 1.
    zero_mean : bool
        Make sure the wavelets are zero mean. Defaults to True.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see mne.verbose).

    Returns
    -------
    power : np.ndarray, shape (n_channels, n_frequencies, n_times)
        Induced power. Squared amplitude of time-frequency coefficients.
    itc : np.ndarray, shape (n_channels, n_frequencies, n_times)
        Phase locking value.
    """
    n_epochs, n_channels, n_times = data[:, :, ::decim].shape
    logger.info('Data is %d trials and %d channels', n_epochs, n_channels)
    n_frequencies = len(frequencies)
    logger.info('Multitaper time-frequency analysis for %d frequencies',
                n_frequencies)

    # Precompute wavelets for given frequency range to save time
    Ws = _dpss_wavelet(sfreq, frequencies, n_cycles=n_cycles,
                       time_bandwidth=time_bandwidth, zero_mean=zero_mean)
    n_taps = len(Ws)
    logger.info('Using %d tapers', n_taps)
    n_times_wavelets = Ws[0][0].shape[0]
    if n_times <= n_times_wavelets:
        warnings.warn("Time windows are as long or longer than the epoch. "
                      "Consider reducing n_cycles.")
    psd = np.zeros((n_channels, n_frequencies, n_times))
    itc = np.zeros((n_channels, n_frequencies, n_times))
    parallel, my_time_frequency, _ = parallel_func(_time_frequency,
                                                   n_jobs)
    for m in range(n_taps):
        psd_itc = parallel(my_time_frequency(data[:, c, :],
                                             Ws[m], use_fft, decim)
                           for c in range(n_channels))
        for c, (psd_c, itc_c) in enumerate(psd_itc):
            psd[c, :, :] += psd_c
            itc[c, :, :] += itc_c
    psd /= n_taps
    itc /= n_taps
    return psd, itc


def tfr_multitaper(inst, freqs, n_cycles, time_bandwidth=4.0, use_fft=True,
                   return_itc=True, decim=1, n_jobs=1):
    """Compute Time-Frequency Representation (TFR) using DPSS wavelets

    Parameters
    ----------
    inst : Epochs | Evoked
        The epochs or evoked object.
    freqs : ndarray, shape (n_freqs,)
        The frequencies in Hz.
    n_cycles : float | ndarray, shape (n_freqs,)
        The number of cycles globally or for each frequency.
        The time-window length is thus T = n_cycles / freq.
    time_bandwidth : float, (optional)
        Time x (Full) Bandwidth product. Should be >= 2.0.
        Choose this along with n_cycles to get desired frequency resolution.
        The number of good tapers (least leakage from far away frequencies)
        is chosen automatically based on this to floor(time_bandwidth - 1).
        Default is 4.0 (3 good tapers).
        E.g., With freq = 20 Hz and n_cycles = 10, we get time = 0.5 s.
        If time_bandwidth = 4., then frequency smoothing is (4 / time) = 8 Hz.
    use_fft : bool
        The fft based convolution or not.
        Defaults to True.
    return_itc : bool
        Return intertrial coherence (ITC) as well as averaged power.
        Defaults to True.
    decim : int
        The decimation factor on the time axis. To reduce memory usage.
        Note than this is brute force decimation, no anti-aliasing is done.
        Defaults to 1.
    n_jobs : int
        The number of jobs to run in parallel. Defaults to 1.

    Returns
    -------
    power : AverageTFR
        The averaged power.
    itc : AverageTFR
        The intertrial coherence (ITC). Only returned if return_itc
        is True.
    """

    data = _get_data(inst, return_itc)
    picks = pick_types(inst.info, meg=True, eeg=True)
    info = pick_info(inst.info, picks)
    data = data[:, picks, :]
    power, itc = _induced_power_mtm(data, sfreq=info['sfreq'],
                                    frequencies=freqs, n_cycles=n_cycles,
                                    time_bandwidth=time_bandwidth,
                                    use_fft=use_fft, decim=decim,
                                    n_jobs=n_jobs, zero_mean=True,
                                    verbose='INFO')
    times = inst.times[::decim].copy()
    nave = len(data)
    out = AverageTFR(info, power, times, freqs, nave,
                     method='mutlitaper-power')
    if return_itc:
        out = (out, AverageTFR(info, itc, times, freqs, nave,
                               method='mutlitaper-itc'))
    return out
