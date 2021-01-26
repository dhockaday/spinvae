import os
import pathlib
import pandas as pd
from multiprocessing import Lock
import time
import json
import torch
import torch.nn as nn
import torch.utils
import torch.fft
import torchaudio.transforms
import torchaudio.functional
import soundfile
import numpy as np
import copy
import sys

from synth import dexed

# See https://github.com/pytorch/audio/issues/903
#torchaudio.set_audio_backend("sox_io")


# Global lock... Should be the same for all forked Unix processes
dexed_vst_lock = Lock()


class DexedDataset(torch.utils.data.Dataset):
    def __init__(self, algos=None, constant_filter_and_tune_params=True, prevent_SH_LFO=True,
                 midi_note=60, midi_velocity=100,  # TODO default values - try others
                 n_fft=1024, fft_hop=256,  # w/ hop=512: spectrogram is approx. the same size as 5.0s@22.05kHz audio
                 n_mel_bins=-1, mel_fmin=30.0, mel_fmax=11e3,
                 normalize_audio=False, spectrogram_min_dB=-200.0, spectrogram_dynamic_range_dB=100.0):
        """
        Allows access to Dexed preset values and names, and generates spectrograms and corresponding
        parameters values. Can manage a reduced number of synth parameters (using default values for non-
        learnable params).

        :param algos: List. Can be used to limit the DX7 algorithms included in this dataset. Set to None
        to use all available algorithms
        :param constant_filter_and_tune_params: if True, the main filter and the main tune settings are default
        :param prevent_SH_LFO: if True, replaces the SH random LFO by a square-wave deterministic LFO
        :param n_mel_bins: Number of frequency bins for the Mel-spectrogram. If -1, the normal STFT will be
        used instead.
        """
        self.spectrogram_dynamic_range_dB = spectrogram_dynamic_range_dB
        self.spectrogram_min_dB = spectrogram_min_dB
        self.normalize_audio = normalize_audio
        self.mel_fmax = mel_fmax
        self.mel_fmin = mel_fmin
        self.n_mel_bins = n_mel_bins
        self.fft_hop = fft_hop
        self.n_fft = n_fft
        self.midi_note = midi_note
        self.midi_velocity = midi_velocity
        self.prevent_SH_LFO = prevent_SH_LFO
        self.constant_filter_and_tune_params = constant_filter_and_tune_params
        self.algos = algos if algos is not None else []
        # - - - Full SQLite DB read and temp storage in np arrays
        dexed_db = dexed.PresetDatabase()
        self.total_nb_presets = dexed_db.presets_mat.shape[0]
        self.total_nb_params = dexed_db.presets_mat.shape[1]
        self.learnable_params_idx = list(range(0, dexed_db.presets_mat.shape[1]))
        if self.constant_filter_and_tune_params:  # (see dexed db exploration notebook)
            for idx in [0, 1, 2, 3, 13]:
                self.learnable_params_idx.remove(idx)
        # All oscillators are always ON (see dexed db exploration notebook)
        for col in [44, 66, 88, 110, 132, 154]:
            self.learnable_params_idx.remove(col)
        # - - - Valid presets - UIDs of presets, and not their database row index
        if len(self.algos) == 0:  # All presets are valid
            self.valid_preset_UIDs = dexed_db.all_presets_df["index_preset"].values
        else:
            if len(self.algos) == 1:
                self.learnable_params_idx.remove(4)  # Algo parameter column idx
            valid_presets_row_indexes = list()
            for algo in self.algos:
                valid_presets_row_indexes += dexed_db.get_preset_indexes_for_algorithm(algo)
            self.valid_preset_UIDs = dexed_db.all_presets_df\
                .iloc[valid_presets_row_indexes]['index_preset'].values
        # DB class deleted (we need a low memory usage for multi-process dataloaders)
        del dexed_db
        # - - - Spectrogram pre-computations
        self.spectrogram_window = torch.hann_window(self.n_fft, periodic=False)
        self.spectrogram_norm_factor = torch.fft.rfft(self.spectrogram_window).abs().max().item()
        # try load spectrogram min/max/mean/std statistics
        try:
            f = open(self._get_spectrogram_stats_file(), 'r')
            self.spectrogram_stats = json.load(f)
        except IOError:
            self.spectrogram_stats = {'mean': 0.0, 'std': 1.0}  # corresponds to no scaling
            print("[DexedDataset] No pre-computed spectrogram stats can be found."
                  " Default 0.0 mean, 1.0 std will be used")

    def __len__(self):
        return len(self.valid_preset_UIDs)

    def get_preset_params(self, preset_UID):
        preset_params = dexed.PresetDatabase.get_preset_params_values_from_file(preset_UID)
        dexed.Dexed.set_all_oscillators_on_(preset_params)
        if self.constant_filter_and_tune_params:
            dexed.Dexed.set_default_general_filter_and_tune_params_(preset_params)
        if self.prevent_SH_LFO:
            dexed.Dexed.prevent_SH_LFO_(preset_params)
        return preset_params

    def get_spectrogram(self, x_wav):
        """ Returns the spectrogram of x_wav. Does not apply mu/sigma (dataset-wide values) normalization. """
        spectrogram = torch.stft(torch.tensor(x_wav, dtype=torch.float32), n_fft=self.n_fft, hop_length=self.fft_hop,
                                 window=self.spectrogram_window, center=True,
                                 pad_mode='constant', onesided=True, return_complex=True).abs()
        # normalization of spectrogram module vs. Hann window weight / min / dynamic dB range
        spectrogram = spectrogram / self.spectrogram_norm_factor
        spectrogram = torch.maximum(spectrogram,
                                    torch.ones(spectrogram.size()) * 10 ** (self.spectrogram_min_dB / 20.0))
        spectrogram = 20.0 * torch.log10(spectrogram)
        spectrogram = torch.maximum(spectrogram, torch.ones(spectrogram.size())
                                    * (torch.max(spectrogram) - self.spectrogram_dynamic_range_dB))
        return spectrogram

    def __getitem__(self, i):
        """ Returns a tuple containing a 2D scaled dB spectrogram tensor (1st dim: freq; 2nd dim: time),
        a 1D tensor of parameter values in [0;1], and a 1d tensor with remaining int info (preset UID, midi note, vel).

        If this dataset generates audio directly from the synth, only 1 dataloader is allowed.
        A 30000 presets dataset require approx. 7 minutes to be generated on 1 CPU. """
        midi_note = self.midi_note
        midi_velocity = self.midi_velocity
        # loading and pre-processing
        preset_UID = self.valid_preset_UIDs[i]
        #preset_name = dexed.PresetDatabase.get_preset_name_from_file(preset_UID)  # debug purposes
        preset_params = self.get_preset_params(preset_UID)
        x_wav, _ = self.get_wav_file(preset_UID, midi_note, midi_velocity)
        spectrogram = (self.get_spectrogram(x_wav) - self.spectrogram_stats['mean'])/self.spectrogram_stats['std']
        # Tuple output. Warning: torch.from_numpy does not copy values
        # We add a first dimension to the spectrogram, which is a 1-ch 'greyscale' image
        return torch.unsqueeze(spectrogram, 0), \
            torch.tensor(preset_params[self.learnable_params_idx], dtype=torch.float32), \
            torch.tensor([preset_UID, midi_note, midi_velocity], dtype=torch.int32)

    def _render_audio(self, preset_params, midi_note, midi_velocity):
        """ Renders audio on-the-fly and returns the computed audio waveform and sampling rate. """
        dexed_renderer = dexed.Dexed()  # reloads the VST to prevent hanging notes/sounds
        dexed_renderer.assign_preset(dexed.PresetDatabase.get_params_in_plugin_format(preset_params))
        x_wav = dexed_renderer.render_note(midi_note, midi_velocity, normalize=self.normalize_audio)
        return x_wav, dexed_renderer.Fs

    def get_spectrogram_tensor_size(self):
        """ Returns the size of the first tensor (2D image) returned by this dataset. """
        dummy_spectrogram, _, _ = self.__getitem__(0)
        return dummy_spectrogram.size()

    def get_param_tensor_size(self):
        """ Returns the length of the second tensor returned by this dataset. """
        return len(self.learnable_params_idx)

    def __str__(self):
        return "Dataset of {}/{} Dexed presets. {} learnable synth params, {} fixed params." \
            .format(len(self), self.total_nb_presets, len(self.learnable_params_idx),
                    self.total_nb_params - len(self.learnable_params_idx))

    @staticmethod
    def _get_spectrogram_stats_folder():
        return pathlib.Path(__file__).parent.joinpath('stats')

    def _get_spectrogram_stats_file(self):
        return self._get_spectrogram_stats_folder().joinpath(
            'DexedDataset_spectrogram_nfft{:04d}hop{:04d}.json'.format(self.n_fft, self.fft_hop))

    def _get_spectrogram_full_stats_file(self):
        return self._get_spectrogram_stats_folder().joinpath(
            'DexedDataset_spectrogram_nfft{:04d}hop{:04d}_full.csv'.format(self.n_fft, self.fft_hop))

    def compute_and_store_spectrograms_stats(self):
        """ Compute min,max,mean,std on all presets previously rendered as wav files.
        Per-preset results are stored into a .csv file
        and dataset-wide averaged results are stored into a .json file

        This functions must be re-run when spectrogram parameters are changed. """
        full_stats = {'UID': [], 'min': [], 'max': [], 'mean': [], 'var': []}
        for i in range(len(self)):
            # We use the exact same spectrogram as the dataloader will
            x_wav, Fs = self.get_wav_file(self.valid_preset_UIDs[i],
                                          self.midi_note, self.midi_velocity)
            assert Fs == 22050
            tensor_spectrogram = self.get_spectrogram(x_wav)
            full_stats['UID'].append(self.valid_preset_UIDs[i])
            full_stats['min'].append(torch.min(tensor_spectrogram).item())
            full_stats['max'].append(torch.max(tensor_spectrogram).item())
            full_stats['var'].append(torch.var(tensor_spectrogram).item())
            full_stats['mean'].append(torch.mean(tensor_spectrogram, dim=(0, 1)).item())
            if i % 5000 == 0:
                print("Processed stats of {}/{} spectrograms".format(i, len(self)))
        for key in full_stats:
            full_stats[key] = np.asarray(full_stats[key])
        # Average of all columns (std: sqrt(variance avg))
        dataset_stats = {'min': full_stats['min'].min(),
                         'max': full_stats['max'].max(),
                         'mean': full_stats['mean'].mean(),
                         'std': np.sqrt(full_stats['var'].mean()) }
        full_stats['std'] = np.sqrt(full_stats['var'])
        del full_stats['var']
        # Final output
        if not os.path.exists(self._get_spectrogram_stats_folder()):
            os.makedirs(self._get_spectrogram_stats_folder())
        full_stats = pd.DataFrame(full_stats)
        full_stats.to_csv(self._get_spectrogram_full_stats_file())
        with open(self._get_spectrogram_stats_file(), 'w') as f:
            json.dump(dataset_stats, f)

    @staticmethod
    def get_wav_file_path(preset_UID, midi_note, midi_velocity):
        presets_folder = dexed.PresetDatabase._get_presets_folder()
        filename = "preset{:06d}_midi{:03d}vel{:03d}.wav".format(preset_UID, midi_note, midi_velocity)
        return presets_folder.joinpath(filename)

    @staticmethod
    def get_wav_file(preset_UID, midi_note, midi_velocity):
        return soundfile.read(DexedDataset.get_wav_file_path(preset_UID, midi_note, midi_velocity))

    def generate_wav_files(self):
        """ Reads all presets from .pickle and .txt files
        (see dexed.PresetDatabase.write_all_presets_to_files(...)) and renders them
         using attributes of this class (midi note, normalization, etc...)

         Floating-point .wav files will be stored in dexed presets' folder (see synth/dexed.py) """
        midi_note, midi_velocity = self.midi_note, self.midi_velocity
        for i in range(len(self)):   # TODO full dataset
            preset_UID = self.valid_preset_UIDs[i]
            preset_params = self.get_preset_params(preset_UID)
            x_wav, Fs = self._render_audio(preset_params, midi_note, midi_velocity)  # Re-Loads the VST
            soundfile.write(self.get_wav_file_path(preset_UID, midi_note, midi_velocity),
                            x_wav, Fs, subtype='FLOAT')
            if i % 5000 == 0:
                print("Writing .wav files... ({}/{})".format(i, len(self)))
        print("Finished writing {} .wav files".format(len(self)))


if __name__ == "__main__":
    dexed_dataset = DexedDataset()
    print(dexed_dataset)

    # Test dataload - to trigger potential errors
    #_, _, _ = dexed_dataset[0]

    # WRITE ALL WAV FILES (approx. 13Go)
    # dexed_dataset.generate_wav_files()

    # stats sur les spectrogrammes de tout le dataset
    dexed_dataset.compute_and_store_spectrograms_stats()

    # Dataloader test
    if False:
        dexed_dataloader = torch.utils.data.DataLoader(dexed_dataset, batch_size=128, shuffle=False,
                                                       num_workers=1)#os.cpu_count() * 9 // 10)
        t0 = time.time()
        for batch_idx, sample in enumerate(dexed_dataloader):
            print(batch_idx)
            print(sample)
            if batch_idx%10 == 0:
                print("batch {}".format(batch_idx))
        print("Full dataset read in {:.1f} minutes.".format((time.time() - t0) / 60.0))
