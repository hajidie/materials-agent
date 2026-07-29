"""Lazy definitions for the original conditional DDPM architecture."""

import math


def cosine_beta_schedule(torch_module, timesteps, s=0.008):
    steps = timesteps + 1
    values = torch_module.linspace(0, timesteps, steps)
    alphas_cumprod = torch_module.cos(
        ((values / timesteps) + s) / (1 + s) * math.pi * 0.5
    ) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch_module.clip(betas, 0.0001, 0.995)


def build_conditional_unet():
    """Build the original network without importing Torch at module import."""
    import torch
    import torch.nn as nn
    import torch.nn.functional as functional

    class SinusoidalPositionEmbeddings(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.dim = dim

        def forward(self, time):
            device = time.device
            half_dim = self.dim // 2
            scale = math.log(10000) / (half_dim - 1)
            weights = torch.exp(
                torch.arange(half_dim, device=device) * -scale
            )
            embeddings = time[:, None] * weights[None, :]
            return torch.cat(
                (embeddings.sin(), embeddings.cos()), dim=-1
            )

    class ConditionEmbeddings(nn.Module):
        def __init__(self, input_dim=4, emb_dim=128):
            super().__init__()
            self.emb_dim = emb_dim
            self.half_dim = emb_dim // (input_dim * 2)
            self.mlp = nn.Sequential(
                nn.Linear(input_dim * self.half_dim * 2, emb_dim),
                nn.GELU(),
                nn.Linear(emb_dim, emb_dim),
            )

        def forward(self, values):
            device = values.device
            values = values * 20.0
            scale = math.log(10000) / (self.half_dim - 1)
            weights = torch.exp(
                torch.arange(self.half_dim, device=device) * -scale
            )
            arguments = (
                values.unsqueeze(-1)
                * weights.unsqueeze(0).unsqueeze(0)
            )
            embedded = torch.cat(
                (torch.sin(arguments), torch.cos(arguments)), dim=-1
            )
            return self.mlp(embedded.view(values.shape[0], -1))

    class ResidualBlock(nn.Module):
        def __init__(
            self,
            in_channels,
            out_channels,
            time_embedding_dim,
            condition_embedding_dim,
            dropout=0.1,
        ):
            super().__init__()
            self.time_mlp = nn.Linear(
                time_embedding_dim, out_channels
            )
            self.cond_mlp = nn.Linear(
                condition_embedding_dim, out_channels
            )
            self.conv1 = nn.Conv2d(
                in_channels, out_channels, 3, padding=1
            )
            self.conv2 = nn.Conv2d(
                out_channels, out_channels, 3, padding=1
            )
            self.bnorm1 = nn.GroupNorm(32, out_channels)
            self.bnorm2 = nn.GroupNorm(32, out_channels)
            self.relu = nn.ReLU()
            self.dropout = nn.Dropout(dropout)
            self.shortcut = (
                nn.Conv2d(in_channels, out_channels, 1)
                if in_channels != out_channels
                else nn.Identity()
            )

        def forward(self, values, time, condition):
            hidden = self.relu(self.bnorm1(self.conv1(values)))
            time_embedding = self.relu(
                self.time_mlp(time)
            )[:, :, None, None]
            condition_embedding = self.relu(
                self.cond_mlp(condition)
            )[:, :, None, None]
            hidden = hidden + time_embedding + condition_embedding
            hidden = self.relu(self.bnorm2(self.conv2(hidden)))
            hidden = self.dropout(hidden)
            return hidden + self.shortcut(values)

    class SelfAttention(nn.Module):
        def __init__(self, channels):
            super().__init__()
            self.channels = channels
            self.mha = nn.MultiheadAttention(
                channels, num_heads=4, batch_first=True
            )
            self.ln = nn.LayerNorm([channels])
            self.ff_self = nn.Sequential(
                nn.LayerNorm([channels]),
                nn.Linear(channels, channels),
                nn.GELU(),
                nn.Linear(channels, channels),
            )

        def forward(self, values):
            size = values.shape[-1]
            reshaped = values.view(
                -1, self.channels, size * size
            ).swapaxes(1, 2)
            normalized = self.ln(reshaped)
            attended, _weights = self.mha(
                normalized, normalized, normalized
            )
            attended = attended + reshaped
            attended = self.ff_self(attended) + attended
            return attended.swapaxes(2, 1).view(
                -1, self.channels, size, size
            )

    class DownSample(nn.Module):
        def __init__(self, channels):
            super().__init__()
            self.op = nn.Conv2d(channels, channels, 4, 2, 1)

        def forward(self, values, _time, _condition):
            return self.op(values)

    class UpSample(nn.Module):
        def __init__(self, in_channels, out_channels):
            super().__init__()
            self.op = nn.ConvTranspose2d(
                in_channels, out_channels, 4, 2, 1
            )

        def forward(self, values, _time, _condition):
            return self.op(values)

    class ConditionalUNet(nn.Module):
        def __init__(
            self,
            image_channels=1,
            down_channels=(64, 128, 256, 512, 1024),
            up_channels=(1024, 512, 256, 128, 64),
            time_embedding_dim=128,
            condition_input_dim=4,
            condition_embedding_dim=128,
            dropout_rate=0.0,
        ):
            super().__init__()
            self.time_mlp = nn.Sequential(
                SinusoidalPositionEmbeddings(time_embedding_dim),
                nn.Linear(time_embedding_dim, time_embedding_dim),
                nn.ReLU(),
            )
            self.cond_mlp = ConditionEmbeddings(
                condition_input_dim, condition_embedding_dim
            )
            self.null_condition = nn.Parameter(
                torch.ones(condition_input_dim) * -1.0
            )
            self.conv0 = nn.Conv2d(
                image_channels, down_channels[0], 3, padding=1
            )
            self.downs = nn.ModuleList([])
            for index in range(len(down_channels) - 1):
                self.downs.append(
                    nn.ModuleList(
                        (
                            ResidualBlock(
                                down_channels[index],
                                down_channels[index],
                                time_embedding_dim,
                                condition_embedding_dim,
                                dropout_rate,
                            ),
                            ResidualBlock(
                                down_channels[index],
                                down_channels[index + 1],
                                time_embedding_dim,
                                condition_embedding_dim,
                                dropout_rate,
                            ),
                            DownSample(down_channels[index + 1]),
                        )
                    )
                )
            middle = down_channels[-1]
            self.mid_block1 = ResidualBlock(
                middle,
                middle,
                time_embedding_dim,
                condition_embedding_dim,
                dropout_rate,
            )
            self.mid_attn = SelfAttention(middle)
            self.mid_block2 = ResidualBlock(
                middle,
                middle,
                time_embedding_dim,
                condition_embedding_dim,
                dropout_rate,
            )
            self.ups = nn.ModuleList([])
            for index in range(len(up_channels) - 1):
                block_input = (
                    up_channels[index + 1] + up_channels[index]
                )
                self.ups.append(
                    nn.ModuleList(
                        (
                            UpSample(
                                up_channels[index],
                                up_channels[index + 1],
                            ),
                            ResidualBlock(
                                block_input,
                                up_channels[index + 1],
                                time_embedding_dim,
                                condition_embedding_dim,
                                dropout_rate,
                            ),
                            ResidualBlock(
                                up_channels[index + 1],
                                up_channels[index + 1],
                                time_embedding_dim,
                                condition_embedding_dim,
                                dropout_rate,
                            ),
                        )
                    )
                )
            self.output = nn.Conv2d(
                up_channels[-1], image_channels, 1
            )

        def forward(self, values, timestep, condition):
            time = self.time_mlp(timestep)
            encoded_condition = self.cond_mlp(condition)
            values = self.conv0(values)
            residuals = []
            for block1, block2, downsample in self.downs:
                values = block1(values, time, encoded_condition)
                values = block2(values, time, encoded_condition)
                residuals.append(values)
                values = downsample(values, time, encoded_condition)
            values = self.mid_block1(values, time, encoded_condition)
            values = self.mid_attn(values)
            values = self.mid_block2(values, time, encoded_condition)
            for upsample, block1, block2 in self.ups:
                values = upsample(values, time, encoded_condition)
                residual = residuals.pop()
                if values.shape[-1] != residual.shape[-1]:
                    values = functional.interpolate(
                        values,
                        size=residual.shape[2:],
                        mode="bilinear",
                        align_corners=False,
                    )
                values = torch.cat((residual, values), dim=1)
                values = block1(values, time, encoded_condition)
                values = block2(values, time, encoded_condition)
            return self.output(values)

    return ConditionalUNet()
