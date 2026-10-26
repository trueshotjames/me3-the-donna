---
license: cc-by-nc-sa-4.0
pipeline_tag: audio-classification
tags:
- wav2small
- valence
- arousal
- dominance
- speech
- speech-emotion-recognition
---

# Wav2Small2.0 - Arousal / Dominance / Valence

Please note that this model is for research purpose only. A commercial [license](https://www.audeering.com/products/devaice/) can be acquired with audEERING. The model expects a raw audio signal 16KHz as input, and outputs: arousal, dominance valence in range [0, 1]. The model is created following the [Wav2Small paper](https://arxiv.org/abs/2408.13920) and has a total of 17K params.


# How To

```python
import torch
import numpy as np
import librosa
from transformers import Wav2Vec2PreTrainedModel, PretrainedConfig
from torch import nn




signal = torch.from_numpy(
    librosa.load('test.wav', sr=16000)[0])[None, :]
device = 'cpu'



def _prenorm(x, attention_mask=None):
    '''wav2vec2'''
    if attention_mask is not None:
        N = attention_mask.sum(1, keepdim=True)  # here attn msk is unprocessed just the original input
        x -= x.sum(1, keepdim=True) / N
        var = (x * x).sum(1, keepdim=True) / N

    else:
        x -= x.mean(1, keepdim=True)  # mean is an onnx operator reducemean saves some ops compared to casting integer N to float and the div
        var = (x * x).mean(1, keepdim=True)
    return x / torch.sqrt(var + 1e-7)




class Spectrogram(nn.Module):
    def __init__(self,
        n_fft=64,   # num cols of DFT
        n_time=64,  # num rows of DFT matrix
        hop_length=32,
        freeze_parameters=True):


        super().__init__()

        fft_window = librosa.filters.get_window('hann', n_time, fftbins=True)

        fft_window = librosa.util.pad_center(fft_window, size=n_time)

        
        
        

        out_channels = n_fft // 2 + 1
        
        (x, y) = np.meshgrid(np.arange(n_time), np.arange(n_fft))
        omega = np.exp(-2 * np.pi * 1j / n_time)
        dft_matrix = np.power(omega, x * y)  # (n_fft, n_time)
        dft_matrix = dft_matrix * fft_window[None, :]
        dft_matrix = dft_matrix[0 : out_channels, :]
        dft_matrix = dft_matrix[:, None, :]
        
        # ---- Assymetric DFT Non Square

        self.conv_real = nn.Conv1d(1, out_channels, n_fft, stride=hop_length,
                                   padding=0, bias=False)
        self.conv_imag = nn.Conv1d(1, out_channels, n_fft, stride=hop_length,
                                   padding=0, bias=False)
        self.conv_real.weight.data = torch.tensor(np.real(dft_matrix),
                                                    dtype=self.conv_real.weight.dtype,
                                                    device=self.conv_real.weight.device)
        self.conv_imag.weight.data = torch.tensor(np.imag(dft_matrix),
                                                    dtype=self.conv_imag.weight.dtype,
                                                    device=self.conv_imag.weight.device)
        if freeze_parameters:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, input):
        x = input[:, None, :]

        real = self.conv_real(x)
        imag = self.conv_imag(x)
        return real ** 2 + imag ** 2  # bs, freq, time-frames


class LogmelFilterBank(nn.Module):
    def __init__(self,
                sr=16000,
                n_fft=64,
                n_mels=26,  # maxpool
                fmin=0.0,
                freeze_parameters=True):

        super().__init__()

        fmax = sr//2

        W2 = librosa.filters.mel(sr=sr,
                                        n_fft=n_fft,
                                        n_mels=n_mels,
                                        fmin=fmin,
                                        fmax=fmax).T

        self.register_buffer('melW', torch.Tensor(W2))
        self.register_buffer('amin', torch.Tensor([1e-10]))

    def forward(self, x):

        x = torch.matmul(x[:, None, :, :].transpose(2, 3), self.melW)   # changes melf not num frames

        x = torch.where(x > self.amin, x, self.amin)  # not in place

        x = 10 * torch.log10(x)
        return x


class Conv(nn.Module):

    def __init__(self, c_in, c_out, k=3, stride=1, padding=1):

        super().__init__()

        self.conv = nn.Conv2d(c_in, c_out, k, stride=stride, padding=padding, bias=False)
        self.norm = nn.BatchNorm2d(c_out)

    def forward(self, x):
        x = self.conv(x)
        x = self.norm(x)
        return torch.relu_(x)





class Vgg7(nn.Module):

    def __init__(self):

        super().__init__()

        self.l1 = Conv( 1, 13)
        self.l2 = Conv(13, 13)
        self.l3 = Conv(13, 13)
        self.maxpool_A = nn.MaxPool2d(3,
                                    stride=2,
                                    padding=1)
        self.l4 = Conv(13, 13)
        self.l5 = Conv(13, 13)
        self.l6 = Conv(13, 13)
        self.l7 = Conv(13, 13)
        self.lin = nn.Conv2d(13, 13, 1, padding=0, stride=1)
        self.sof = nn.Conv2d(13, 13, 1, padding=0, stride=1)
        self.spectrogram_extractor = Spectrogram()
        self.logmel_extractor = LogmelFilterBank()

    def forward(self, x, attention_mask=None):
        x = _prenorm(x, attention_mask=attention_mask)
        x = self.spectrogram_extractor(x)
        x = self.logmel_extractor(x)
        x = self.l1(x)
        x = self.l2(x)
        x = self.l3(x)
        x = self.maxpool_A(x)  # reshape here? so these conv will have large kernel
        x = self.l4(x)
        x = self.l5(x)
        x = self.l6(x)
        x = self.l7(x)
        x = self.lin(x) * self.sof(x).softmax(2)   # [bs, ch, time-frams, mel]
        x = x.sum(2)
        x = torch.cat([x,
                        torch.bmm(x, x.transpose(1,2))], 2)  # cosine over mel dims
        return x.reshape(-1, 338)


class Wav2SmallConfig(PretrainedConfig):
    model_type = "wav2vec2"

    def __init__(self,
                **kwargs):
        super().__init__(**kwargs)
        self.half_mel = 13
        self.n_fft = 64
        self.n_time = 64
        self.hidden = 2 * self.half_mel * self.half_mel
        self.hop = self.n_time // 2


class Wav2Small(Wav2Vec2PreTrainedModel):

    def __init__(self,
                config):
        super().__init__(config)
        self.vgg7 = Vgg7()
        self.adv  = nn.Linear(config.hidden, 3)   # 0=arousal, 1=dominance, 2=valence

    def forward(self, x, attention_mask=None):
        x = self.vgg7(x, attention_mask=attention_mask)
        return self.adv(x)


model = Wav2Small.from_pretrained(
    'audeering/wav2small').to(device).eval()
with torch.no_grad():
    logits = model(signal.to(device))

print(f'\nArousal={logits[:, 0]}\n',
      f'Dominance={logits[:, 1]}\n',
      f'Valence={logits[:, 2]}\n')
```
