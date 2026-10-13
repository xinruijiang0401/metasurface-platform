"""Figures made exclusively from saved numerical results; no mock metrics."""
from __future__ import annotations

from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                    "figure.facecolor": "white", "savefig.facecolor": "white"})


def field_plot(path, data, x, y, title, label, *, phase=False, mask=None,
               xlabel="x (um)", ylabel="y (um)"):
    value = np.ma.array(np.asarray(data), mask=~mask if mask is not None else False)
    fig, ax = plt.subplots(figsize=(6.8, 5.2), layout="constrained")
    opts = {"cmap": "twilight", "vmin": -np.pi, "vmax": np.pi} if phase else {"cmap": "viridis"}
    im = ax.imshow(value, extent=[x[0], x[-1], y[0], y[-1]], origin="lower", aspect="auto", **opts)
    ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
    fig.colorbar(im, ax=ax, label=label)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def library_plot(path, candidates, selected):
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), layout="constrained")
    for c in candidates:
        t = c["jones_t"][:, 0, 0]
        label = f"{c['source_id']} / p{c['period_nm']:g} h{c['height_nm']:g}"
        width = 2.4 if c["candidate_id"] == selected["candidate_id"] else 0.9
        axes[0].plot(2*c["radii_nm"], np.angle(t), ".-", lw=width, label=label)
        axes[1].plot(2*c["radii_nm"], c["power_T"][:, 0], ".-", lw=width)
    axes[0].set(xlabel="Diameter (nm)", ylabel="arg(t_xx) (rad)", title="Direct RCWA / electric-field phase")
    axes[1].set(xlabel="Diameter (nm)", ylabel="Flux transmission", title="Power normalization includes n_out/n_in", ylim=(0, 1.05))
    axes[0].legend(fontsize=6)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def axial_plot(path, data, transverse_um, z_um, title):
    fig, ax = plt.subplots(figsize=(7.5, 4.5), layout="constrained")
    im = ax.imshow(data.T, extent=[z_um[0], z_um[-1], transverse_um[0], transverse_um[-1]],
                   origin="lower", aspect="auto", cmap="magma")
    ax.set(xlabel="z from pillar exit (um)", ylabel="Transverse coordinate (um)", title=title)
    fig.colorbar(im, ax=ax, label="Power-density proxy (reference scale)")
    fig.savefig(path, dpi=140)
    plt.close(fig)


def tolerance_plot(path, tolerance):
    samples = [s["focusing_efficiency"] for s in tolerance["samples"] if s["status"] == "evaluated"]
    fig, ax = plt.subplots(figsize=(6.8, 4.5), layout="constrained")
    if samples:
        ax.hist(samples, bins=max(3, min(10, len(samples))), color="#23796f", edgecolor="white")
    else:
        ax.text(.5, .5, "All samples rejected: no extrapolation", ha="center", transform=ax.transAxes)
    ax.set(xlabel="Focusing efficiency / incident aperture power", ylabel="Sample count", title="Assumed errors; not calibrated fabrication yield")
    fig.savefig(path, dpi=140)
    plt.close(fig)


def convergence_plot(path, validations):
    fig, axes = plt.subplots(1, 2, figsize=(9.5, 4), layout="constrained")
    for v in validations:
        rows = v["records"]
        label = f"r={v['radius_nm']:g} nm"
        axes[0].plot(range(len(rows)), [r["phase_rad"][0] for r in rows], ".-", label=label)
        axes[1].plot(range(len(rows)), [r["power_T"][0] for r in rows], ".-")
    for ax in axes:
        ax.set_xticks([0,1,2], ["81 / 128", "121 / 128", "121 / 192"])
        ax.set_xlabel("Requested harmonics / pixels per cell")
    axes[0].set_ylabel("Phase relative to empty stack (rad)")
    axes[1].set_ylabel("Flux transmission")
    axes[0].legend(fontsize=7)
    fig.savefig(path, dpi=140)
    plt.close(fig)
