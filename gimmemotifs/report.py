# Copyright (c) 2009-2019 Simon van Heeringen <simon.vanheeringen@gmail.com>
#
# This module is free software. You can redistribute it and/or modify it under
# the terms of the MIT License, see the file COPYING included with this
# distribution.
"""Reports (graphical and text) for motifs statistics."""
import os
import sys
from datetime import datetime
from multiprocessing import Pool
import re
import shutil
import logging

import jinja2
import numpy as np
import pandas as pd
from statsmodels.stats.multitest import multipletests
from pandas.core.indexing import _non_reducing_slice
from pandas.io.formats.style import Styler
import seaborn as sns

try:
    import emoji
except ImportError:
    pass

from gimmemotifs.comparison import MotifComparer
from gimmemotifs.fasta import Fasta
from gimmemotifs.motif import read_motifs
from gimmemotifs.config import MotifConfig, DIRECT_NAME, INDIRECT_NAME
from gimmemotifs.plot import roc_plot
from gimmemotifs.stats import calc_stats, add_star, write_stats
from gimmemotifs import __version__
from gimmemotifs.utils import motif_localization

logger = logging.getLogger("gimme.report")

FACTOR_TOOLTIP = "<div title='\"Direct\" means that there is direct evidence of binding or that this assignment is based on curated information. \"Predicted\" means that the motif comes from a non-curated ChIP-seq experiment or that the factor was computationally predicted to bind this motif based on its DNA binding domain.'>factors<br/>(<span style='color:black'>direct</span> or <span style='color:#666666'>predicted</span>)</div>"


def _wrap_html_str(x):
    if " " not in x:
        return x

    min_pos, max_pos = 0, len(x)
    if ">" in x and "</" in x:
        m = re.compile(r">[^<>]*<").search(x)
        min_pos, max_pos = m.start(), m.end()

    positions = [m.start() for m in re.compile(" ").finditer(x)]
    positions = [p for p in positions if min_pos < p < max_pos]

    pos = sorted(positions, key=lambda p: abs(p - len(x) / 2))[0]
    x = x[:pos] + "<br/>" + x[pos + 1 :]
    return x


class ExtraStyler(Styler):
    """
    Extra styles for a DataFrame or Series based on pandas.styler using HTML and CSS.
    """

    loader = jinja2.ChoiceLoader(
        [jinja2.FileSystemLoader(MotifConfig().get_template_dir()), Styler.loader]
    )
    env = jinja2.Environment(loader=loader)
    template = env.get_template("table.tpl")

    def __init__(self, *args, **kwargs):
        self._data_todo = []
        self.circle_styles = None
        self.palette_styles = None
        self.col_heading_style = {
            "name": "col_heading",
            "props": [("border-bottom", "1px solid #e0e0e0")],
        }
        super(ExtraStyler, self).__init__(*args, **kwargs)
        self.display_data = self.data.copy()

        # self.template =

        self._font = "Nunito Sans"

    @property
    def font(self):
        return self._font

    @font.setter
    def font(self, font_name):
        self._font = font_name

    def set_font(self, font_name):
        """
        Set the font that will be used.

        Parameters
        ----------
        font_name : str
            Should be a font name available though the Google Font API.

        Returns
        -------
        self : ExtraStyler

        Notes
        -----
        ``font_name`` can contain spaces, eg. "Nunito Sans".

        Examples
        --------
        >>> df = pd.DataFrame(np.random.randn(4, 2), columns=['a', 'b'])
        >>> ExtraStyler(df).font("Roboto)
        """
        self.font = font_name
        return self

    def _current_index(self, subset):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)
        selected = self.data.loc[subset]
        idx_slice = pd.IndexSlice[
            self.data.index.get_indexer(selected.index),
            self.data.columns.get_indexer(selected.columns),
        ]
        return idx_slice

    def _translate(self):
        self._compute_data()
        d = super()._translate()
        circle_styles = self.circle_styles or []
        palette_styles = self.palette_styles or []
        col_heading_style = self.col_heading_style or []
        d.update(
            {
                "font": self.font,
                "circle_styles": circle_styles,
                "palette_styles": palette_styles,
                "col_heading_style": col_heading_style,
            }
        )
        return d

    def _compute_data(self):
        r = self
        for func, args, kwargs in self._data_todo:
            r = func(self)(*args, **kwargs)
        r.data = r.display_data
        return r

    def _tooltip(self, tip, subset=None, part=None):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)

        if part is None:
            part = "data"

        if part == "data":
            self.display_data.loc[subset] = (
                "<div title='"
                + tip
                + "'>"
                + self.display_data.loc[subset].astype(str)
                + "</div>"
            )
        elif part == "columns":
            idx = self._current_index(subset)[1]
            rename = dict(
                zip(
                    self.display_data.columns[idx],
                    "<div title='"
                    + tip
                    + "'>"
                    + self.display_data.columns[idx].astype(str)
                    + "</div>",
                )
            )
            self.display_data.rename(columns=rename, inplace=True)
        elif part == "index":
            idx = self._current_index(subset)[0]
            rename = dict(
                zip(
                    self.display_data.index[idx],
                    "<div title='"
                    + tip
                    + "'>"
                    + self.display_data.index[idx].astype(str)
                    + "</div>",
                )
            )
            self.display_data.rename(index=rename, inplace=True)
        else:
            raise ValueError(f"unknown value for part: {part}")
        return self

    def _wrap_iterable(self, it):
        return [_wrap_html_str(val) for val in it]

    def _wrap(self, subset=None, axis=0):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)

        if axis in [0, "columns"]:
            idx = self._current_index(subset)[1]
            rename = dict(
                zip(
                    self.display_data.columns[idx],
                    self._wrap_iterable(self.display_data.columns[idx]),
                )
            )
            self.display_data.rename(columns=rename, inplace=True)
        elif axis in [1, "index"]:
            idx = self._current_index(subset)[0]
            rename = dict(
                zip(
                    self.display_data.index[idx],
                    self._wrap_iterable(self.display_data.index[idx]),
                )
            )
            self.display_data.rename(index=rename, inplace=True)
        else:
            raise ValueError(f"unknown value for axis: {axis}")
        return self

    def _convert_to_image(self, subset=None, height=30):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)

        self.display_data.loc[subset] = (
            f'<div style="height:{height}px;object-fit:contain;"><img src="'
            + self.data.loc[subset].astype(str)
            + '" style="height:100%;width:100%;object-fit:contain;"/></div>'
        )
        return self

    def _border(self, idx, location="left"):
        return [f"border-{location}: 2px solid #444;" for val in idx]

    def border(
        self,
        subset=None,
        location="bottom",
        part="data",
        width="2px",
        style="solid",
        color="#444",
    ):
        """
        Add a border to data cells, columns or index.

        Parameters
        ----------
        subset : IndexSlice, optional
            An argument to ``DataFrame.loc`` that restricts which elements
            ``border`` is applied to. If ``part`` is "columns" or "index"
            subset should be present in either the columns or the index.

        location : str, optional
            Location of the border, default is "bottom". Can be "top", "bottom",
            "right" or "left".

        part : str, optional
            If ``part`` is "data", the border will be applied to the data cells.
            Set part to "index" or to "column" to add a border to the index or
            header, respectively.

        width : str, int or float, optional
            Valid CSS value for border width.

        style : str,  optional
            Valid CSS value for border style.

        color : str,  optional
            Valid CSS value for border color.

        Returns
        -------
        self : ExtraStyler

        Examples
        --------
        >>> df = pd.DataFrame(np.random.randn(4, 2), columns=['a', 'b'])
        >>> ExtraStyler(df).border(part="columns)
        """
        if part == "data":
            self.apply(self._border, subset=subset, location=location)
        else:
            self.col_heading_style["props"].append(
                (f"border-{location}", f"{width} {style} {color}")
            )
        return self

    def _align(self, idx, location="center"):
        return [f"text-align:{location};" for val in idx]

    def align(self, subset=None, location="center", axis=0):
        """
        Align text.

        Parameters
        ----------
        subset : IndexSlice, optional
            An argument to ``DataFrame.loc`` that restricts which elements
            ``center_align`` is applied to.

        location : str, optional
            "center", "left" or "right"

        axis : {0 or 'index', 1 or 'columns', None}, default 0
            Apply to each column (``axis=0`` or ``'index'``), to each row
            (``axis=1`` or ``'columns'``), or to the entire DataFrame at once
            with ``axis=None``.

        Returns
        -------
        self : ExtraStyler
        """
        self.apply(self._align, subset=subset, location=location, axis=axis)
        return self

    def scaled_background_gradient(
        self, subset=None, cmap="RdBu_r", scale_factor=1, center_zero=True
    ):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)
        absmax = np.max(
            (
                abs(self.data.loc[subset].max().max()),
                abs(self.data.loc[subset].min().min()),
            )
        )
        target = absmax * scale_factor
        r = self
        for col in self.data.loc[subset].columns:
            smin = self.data[col].min()
            smax = self.data[col].max()
            diff = smax - smin

            if center_zero:
                # Make sure center of palette is at 0
                low = abs((-target - smin) / diff)
                high = (target - smax) / diff
            else:
                high = 1 / scale_factor
                low = 1 / scale_factor

            r = r.background_gradient(cmap=cmap, low=low, high=high, subset=[col])
        return r

    def _circle(
        self,
        subset=None,
        show_text=True,
        color=None,
        palette=None,
        vmin=None,
        vmax=None,
        scale=False,
        size=25,
        min_size=5,
        morph=False,
    ):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subslice = _non_reducing_slice(subset)
        # Make sure we don't select text columns
        if scale or morph:
            subslice = pd.IndexSlice[
                self.data.loc[subset].index,
                self.data.loc[subset].select_dtypes(exclude=["object"]).columns,
            ]

        self.circle_styles = self.circle_styles or []
        circle_id = len(self.circle_styles) + 1

        props = [
            ("height", f"{size}px"),
            ("width", f"{size}px"),
            ("border-radius", "50%"),
            ("color", "#000"),
            ("line-height", f"{size}px"),
            ("display", "inline-block"),
            ("text-align", "center"),
            ("vertical-align", "middle"),
        ]

        if color:
            palette = sns.color_palette([color])
            # print(palette)
        elif palette is None:
            palette = sns.light_palette((210, 90, 60), input="husl", n_colors=10)
        else:
            # if isinstance(palette, str):
            palette = sns.color_palette(palette)

        self.circle_styles.append({"name": f"circle{circle_id}", "props": props})
        self.palette_styles = self.palette_styles or []
        for i, color in enumerate(palette.as_hex()):
            props = [("background-color", color)]
            if scale:
                circle_size = min_size + ((size - min_size) / len(palette) * (i + 1))
                props += [
                    ("height", f"{circle_size}px"),
                    ("width", f"{circle_size}px"),
                    ("line-height", f"{circle_size}px"),
                    ("text-align", "center"),
                ]
            if morph:
                props += [("border-radius", f"{50 - int(50 / len(palette)) * i}%")]
            self.palette_styles.append(
                {"name": f"color{circle_id}_{i}", "props": props}
            )

        if scale or morph:
            vmax = vmax or self.data.loc[subslice].max().max() * 1.01
            text = self.display_data.loc[subslice].astype(str) if show_text else ""
            self.display_data.loc[subslice] = (
                f"<div class='circle{circle_id} color{circle_id}_"
                + (self.data.loc[subslice] / (vmax / len(palette)))
                .astype(int)
                .astype(str)
                + "'>"
                + text
                + "</div>"
            )
        else:
            text = self.display_data.loc[subslice].astype(str) if show_text else ""
            self.display_data.loc[subslice] = (
                f"<div class='circle{circle_id} color{circle_id}_0'>" + text + "</div>"
            )

        return self

    def add_circle(self, **kwargs):
        self._data_todo.append((lambda instance: instance._circle, (), kwargs))
        return self

    def wrap(self, **kwargs):
        self._data_todo.append((lambda instance: instance._wrap, (), kwargs))
        return self

    def add_tooltip(self, tip, **kwargs):
        self._data_todo.append((lambda instance: instance._tooltip, (tip,), kwargs))
        return self

    def convert_to_image(self, **kwargs):
        self._data_todo.append(
            (lambda instance: instance._convert_to_image, (), kwargs)
        )
        return self

    def rename(self, columns=None, index=None):
        self.display_data = self.display_data.rename(columns=columns, index=index)
        return self

    def _emoji_score(self, series, emoji_str=None, bins=None):
        if emoji_str is None:
            emoji_str = ":star:"
        if bins is None:
            bins = 3

        if isinstance(bins, int):
            labels = range(1, bins + 1)
        else:
            labels = range(1, len(bins))

        return [
            emoji.emojize(emoji_str * val, use_aliases=True)
            for val in pd.cut(series, bins=bins, labels=labels)
        ]

    def _emoji_scale(self, series, emojis=None, bins=None):
        emoji_dict = {
            "thumbs": [":thumbsdown:", ":thumbsup:"],
            "check": [":cross_mark:", ":white_check_mark:"],
            "smiley": [
                ":crying_face:",
                ":slightly_frowning_face:",
                ":neutral_face:",
                ":slightly_smiling_face:",
                ":grin:",
            ],
            "black_square": [
                ":black_small_square:",
                ":black_medium_small_square:",
                ":black_medium_square:",
                ":black_large_square:",
            ],
            "white_square": [
                ":white_small_square:",
                ":white_medium_small_square:",
                ":white_medium_square:",
                ":white_large_square:",
            ],
        }

        if emojis is None:
            emojis = "smiley"

        if emojis in emoji_dict:
            labels = emoji_dict[emojis]
        if bins is None:
            bins = len(labels)

        return [
            emoji.emojize(val, use_aliases=True)
            for val in pd.cut(series, bins=bins, labels=labels)
        ]

    def emoji_scale(self, subset=None, emojis=None, bins=None, axis=0):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)

        idx = self._current_index(subset=subset)

        result = self.display_data.iloc[idx].apply(
            self._emoji_scale, axis=axis, result_type="expand", args=(emojis, bins)
        )
        self.display_data.iloc[idx] = result.values

        return self.align(subset=subset, location="center", axis=axis)

    def emoji_score(self, subset=None, emoji_str=None, bins=None, axis=0):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)

        idx = self._current_index(subset=subset)
        result = self.display_data.iloc[idx].apply(
            self._emoji_score, axis=axis, result_type="expand", args=(emoji_str, bins)
        )
        self.display_data.iloc[idx] = result.values

        return self.align(subset=subset, location="left", axis=axis)

    def emojify(self, subset=None):
        subset = pd.IndexSlice[:, :] if subset is None else subset
        subset = _non_reducing_slice(subset)

        idx = self._current_index(subset=subset)
        result = self.display_data.iloc[idx].applymap(emoji.emojize)
        self.display_data.iloc[idx] = result.values

        return self


def get_roc_values(motif, fg_file, bg_file, genome):
    """Calculate ROC AUC values for ROC plots."""
    try:
        stats = calc_stats(
            fg_file=fg_file,
            bg_file=bg_file,
            motifs=motif,
            genome=genome,
            stats=["roc_values"],
            ncpus=1,
        )
        (x, y) = list(stats.values())[0]["roc_values"]
        return None, x, y
    except Exception as e:
        print(motif)
        print(motif.id)
        print(str(e))
        raise


def create_roc_plots(pfmfile, fgfa, background, outdir, genome):
    """Make ROC plots for all motifs."""
    motifs = read_motifs(pfmfile, fmt="pwm", as_dict=True)
    ncpus = int(MotifConfig().get_default_params()["ncpus"])
    pool = Pool(processes=ncpus)
    jobs = {}
    for bg, fname in background.items():
        for m_id, m in motifs.items():

            k = "{}_{}".format(str(m), bg)
            jobs[k] = pool.apply_async(
                get_roc_values, (motifs[m_id], fgfa, fname, genome)
            )
    imgdir = os.path.join(outdir, "images")
    if not os.path.exists(imgdir):
        os.mkdir(imgdir)

    roc_img_file = os.path.join(outdir, "images", "{}_roc.{}.png")

    for motif in motifs.values():
        for bg in background:
            k = "{}_{}".format(str(motif), bg)
            error, x, y = jobs[k].get()
            if error:
                logger.error("Error in thread: %s", error)
                logger.error("Motif: %s", motif)
                sys.exit(1)
            roc_plot(roc_img_file.format(motif.id, bg), x, y)


def _create_text_report(inputfile, motifs, closest_match, stats, outdir):
    """Create text report of motifs with statistics and database match."""
    my_stats = {}
    for motif in motifs:
        match = closest_match[motif.id]
        my_stats[str(motif)] = {}
        for bg in list(stats.values())[0].keys():
            if str(motif) not in stats:
                logger.error("####")
                logger.error("{} not found".format(str(motif)))
                for s in sorted(stats.keys()):
                    logger.error(s)
                logger.error("####")
            else:
                my_stats[str(motif)][bg] = stats[str(motif)][bg].copy()
                my_stats[str(motif)][bg]["best_match"] = "_".join(
                    match[0].split("_")[:-1]
                )
                my_stats[str(motif)][bg]["best_match_pvalue"] = match[1][-1]

    header = ("# GimmeMotifs version {}\n" "# Inputfile: {}\n").format(
        __version__, inputfile
    )

    write_stats(my_stats, os.path.join(outdir, "stats.{}.txt"), header=header)


def _create_graphical_report(
    inputfile, pwm, background, closest_match, outdir, stats, best_id=None
):
    """Create main gimme_motifs output html report."""
    if best_id is None:
        best_id = {}

    logger.debug("Creating graphical report")

    class ReportMotif(object):
        """Placeholder for motif stats."""

        pass

    config = MotifConfig()

    imgdir = os.path.join(outdir, "images")
    if not os.path.exists(imgdir):
        os.mkdir(imgdir)

    motifs = read_motifs(pwm, fmt="pfm")

    roc_img_file = "%s_roc.%s"

    dbpwm = config.get_default_params()["motif_db"]
    pwmdir = config.get_motif_dir()

    dbmotifs = read_motifs(os.path.join(pwmdir, dbpwm), as_dict=True)

    report_motifs = []
    for motif in motifs:

        rm = ReportMotif()
        rm.id = motif.id
        rm.id_href = {"href": "#%s" % motif.id}
        rm.id_name = {"name": motif.id}
        rm.img = {"src": os.path.join("images", "%s.png" % motif.id)}
        motif.plot_logo(fname=os.path.join(outdir, "images/{}.png".format(motif.id)))

        # TODO: fix best ID
        rm.best = "Gimme"  # best_id[motif.id]

        rm.consensus = motif.to_consensus()
        rm.stars = int(
            np.mean([stats[str(motif)][bg].get("stars", 0) for bg in background]) + 0.5
        )

        rm.bg = {}
        for bg in background:
            rm.bg[bg] = {}
            this_stats = stats.get(str(motif), {}).get(bg)
            # TODO: fix these stats
            rm.bg[bg]["e"] = "%0.2f" % this_stats.get("enr_at_fpr", 1.0)
            rm.bg[bg]["p"] = "%0.2f" % this_stats.get("phyper_at_fpr", 1.0)
            rm.bg[bg]["auc"] = "%0.3f" % this_stats.get("roc_auc", 0.5)
            rm.bg[bg]["mncp"] = "%0.3f" % this_stats.get("mncp", 1.0)
            rm.bg[bg]["roc_img"] = {
                "src": "images/"
                + os.path.basename(roc_img_file % (motif.id, bg))
                + ".png"
            }
            rm.bg[bg]["roc_img_link"] = {
                "href": "images/"
                + os.path.basename(roc_img_file % (motif.id, bg))
                + ".png"
            }

        rm.histogram_img = {"data": "images/%s_histogram.svg" % motif.id}
        rm.histogram_link = {"href": "images/%s_histogram.svg" % motif.id}

        match_id = closest_match[motif.id][0]
        dbmotifs[match_id].plot_logo(
            fname=os.path.join(outdir, "images/{}.png".format(match_id))
        )

        rm.match_img = {"src": "images/{}.png".format(match_id)}
        rm.match_id = closest_match[motif.id][0]
        rm.match_pval = "%0.2e" % closest_match[motif.id][1][-1]

        report_motifs.append(rm)

    total_report = os.path.join(outdir, "gimme.denovo.html")

    star_img = os.path.join(config.get_template_dir(), "star.png")
    shutil.copyfile(star_img, os.path.join(outdir, "images", "star.png"))

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader([config.get_template_dir()])
    )
    template = env.get_template("report_template.jinja.html")
    # TODO: title
    result = template.render(
        motifs=report_motifs,
        inputfile=inputfile,
        date=datetime.today().strftime("%d/%m/%Y"),
        version=__version__,
        bg_types=list(background.keys()),
    )

    with open(total_report, "wb") as f:
        f.write(result.encode("utf-8"))


def create_denovo_motif_report(
    inputfile, pfmfile, fgfa, background, locfa, outdir, params, stats=None
):
    """Create text and graphical (.html) motif reports."""
    logger.info("creating de novo reports")

    motifs = read_motifs(pfmfile, fmt="pwm")

    # ROC plots
    create_roc_plots(pfmfile, fgfa, background, outdir, params["genome"])

    # Closest match in database
    mc = MotifComparer()
    closest_match = mc.get_closest_match(motifs)

    if stats is None:
        stats = {}
        for bg, bgfa in background.items():
            for m, s in calc_stats(fg_file=fgfa, bg_file=bgfa, motifs=motifs).items():
                if m not in stats:
                    stats[m] = {}
                stats[m][bg] = s

    stats = add_star(stats)

    if not params:
        params = {}
    cutoff_fpr = params.get("cutoff_fpr", 0.9)
    lsize = np.median([len(seq) for seq in Fasta(locfa).seqs])

    # Location plots
    logger.debug("Creating localization plots")
    for motif in motifs:
        logger.debug("  {} {}".format(motif.id, motif))
        outfile = os.path.join(outdir, "images/{}_histogram.svg".format(motif.id))
        motif_localization(locfa, motif, lsize, outfile, cutoff=cutoff_fpr)

    # Create reports
    _create_text_report(inputfile, motifs, closest_match, stats, outdir)
    _create_graphical_report(
        inputfile, pfmfile, background, closest_match, outdir, stats
    )


def format_factors(motif, max_length=5):
    fmt_d = "<span style='color:black'>{}</span>"
    fmt_i = "<span style='color:#666666'>{}</span>"

    direct = sorted(list(set([x.upper() for x in motif.factors[DIRECT_NAME]])))
    indirect = sorted(
        list(
            set(
                [
                    x.upper()
                    for x in motif.factors[INDIRECT_NAME]
                    if x.upper() not in direct
                ]
            )
        )
    )

    if len(direct) > max_length:
        show_factors = direct[:max_length]
    else:
        show_factors = direct[:]
        for f in indirect:
            if f not in show_factors:
                show_factors.append(f)
            if len(show_factors) >= max_length:
                break
    show_factors = sorted(show_factors)

    factor_str = ",".join(
        [fmt_d.format(f) if f in direct else fmt_i.format(f) for f in show_factors]
    )

    if len(direct + indirect) > max_length:
        factor_str += ", (...)"

    tooltip = ""
    if len(direct) > 0:
        tooltip += "direct: " + ",".join(sorted(direct))
    if len(indirect) > 0:
        if tooltip != "":
            tooltip += "&#10;"
        tooltip += "predicted: " + ",".join(sorted(indirect))

    factor_str = '<div title="' + tooltip + '">' + factor_str + "</div>"

    return factor_str


def motif_to_factor_series(series, pfmfile=None, motifs=None):
    if motifs is None:
        motifs = read_motifs(pfmfile, as_dict=True)

    if isinstance(series, pd.Index):
        index = series
    else:
        index = series.index

    factors = [format_factors(motifs[motif]) for motif in series]
    return pd.Series(data=factors, index=index)


def motif_to_img_series(series, pfmfile=None, motifs=None, outdir=".", subdir="logos"):
    if motifs is None:
        motifs = read_motifs(pfmfile, as_dict=True)

    if not os.path.exists(outdir):
        os.makedirs(outdir)
    if not os.path.exists(os.path.join(outdir, subdir)):
        os.makedirs(os.path.join(outdir, subdir))

    img_series = []
    for motif in series:
        if motif not in motifs:
            raise ValueError(f"Motif {motif} does not occur in motif database")
        fname = subdir + "/{}.png".format(re.sub("[()/]", "_", motif))
        if not os.path.exists(fname):
            motifs[motif].plot_logo(fname=os.path.join(outdir, fname))
        img_series.append(fname)

    if isinstance(series, pd.Index):
        index = series
    else:
        index = series.index
    return pd.Series(data=img_series, index=index)


def maelstrom_html_report(outdir, infile, pfmfile=None, threshold=4):

    # Read the maelstrom text report
    df = pd.read_table(infile, index_col=0)

    # Columns with maelstrom rank aggregation value
    value_cols = df.columns[
        ~df.columns.str.contains("correlation") & ~df.columns.isin(["% with motif"])
    ]
    # Columns with correlation values
    corr_cols = df.columns[df.columns.str.contains("correlation")]

    df = df[np.any(abs(df[value_cols]) >= threshold, 1)]

    # Add motif logo's
    df.insert(
        0,
        "logo",
        motif_to_img_series(df.index, pfmfile=pfmfile, outdir=outdir, subdir="logos"),
    )
    # Add factors that can bind to the motif
    df.insert(0, "factors", motif_to_factor_series(df.index, pfmfile=pfmfile))

    rename_columns = {"factors": FACTOR_TOOLTIP}

    df_styled = (
        ExtraStyler(df)
        .set_precision(2)
        .convert_to_image(subset=["logo"], height=30,)
        .scaled_background_gradient(
            subset=value_cols, center_zero=True, scale_factor=1.75
        )
        .border(subset=list(value_cols[:1]), location="left")
        .border(part="columns", location="bottom")
        .set_table_attributes('class="sortable-theme-slick" data-sortable')
        .center_align(subset=list(value_cols))
        .set_font("Nunito Sans")
        .rename(columns=rename_columns)
    )

    if len(corr_cols) > 0:
        df_styled = (
            df_styled.wrap(subset=list(corr_cols))
            .center_align(subset=list(corr_cols))
            .scaled_background_gradient(
                subset=corr_cols, cmap="PuOr_r", center_zero=True, scale_factor=1.75
            )
        )

    if "% with motif" in df.columns:
        df["% with motif"] = df["% with motif"].astype(int)
        df_styled = (
            df_styled.add_circle(
                subset=["% with motif"], palette="Purples", vmax=100, size=40
            )
            .wrap(subset=["% with motif"])
            .center_align(subset=["% with motif"])
            .border(subset=["% with motif"], location="left")
        )

    df_styled = df_styled.render()

    with open(outdir + "/gimme.maelstrom.report.html", "w", encoding="utf-8") as f:
        f.write(df_styled)


def roc_html_report(
    outdir,
    infile,
    pfmfile,
    outname="gimme.motifs.html",
    threshold=0.01,
    use_motifs=None,
    link_matches=False,
):
    df = pd.read_table(infile, index_col=0)
    df.rename_axis(None, inplace=True)
    df["corrected P-value"] = multipletests(df["P-value"], method="fdr_bh")[1]

    cols = [
        "logo",
        "# matches",
        "# matches background",
        "P-value",
        "log10 P-value",
        "corrected P-value",
        "ROC AUC",
        "PR AUC",
        "Enr. at 1% FPR",
        "Recall at 10% FDR",
    ]

    motifs = read_motifs(pfmfile)
    if use_motifs is not None:
        motifs = [m for m in motifs if m.id in use_motifs]

    idx = [motif.id for motif in motifs]
    df = df.loc[idx]

    # Add factors that can bind to the motif
    df.insert(0, "factors", motif_to_factor_series(df.index, pfmfile=pfmfile))
    cols = ["factors"] + cols

    df = df[df["corrected P-value"] <= threshold]

    if link_matches:
        df["# matches"] = (
            "<a href=motif_scan_results/"
            + df.index.to_series()
            + ".matches.bed>"
            + df["# matches"].astype(str)
            + "</a>"
        )

    df["logo"] = [
        '<img src="logos/{}.png" height=40/>'.format(re.sub(r"[^-_\w]+", "_", x))
        for x in list(df.index)
    ]

    df = df[cols]

    df = df.rename(columns={"factors": FACTOR_TOOLTIP})
    if not os.path.exists(outdir + "/logos"):
        os.makedirs(outdir + "/logos")
    for motif in motifs:
        if motif.id in df.index:
            motif.plot_logo(
                fname=outdir
                + "/logos/{}.png".format(re.sub(r"[^-_\w]+", "_", motif.id))
            )

    bar_cols = [
        "log10 P-value",
        "ROC AUC",
        "PR AUC",
        "Enr. at 1% FPR",
        "Recall at 10% FDR",
    ]
    template_dir = MotifConfig().get_template_dir()
    js = open(
        os.path.join(template_dir, "sortable/sortable.min.js"), encoding="utf-8"
    ).read()
    css = open(
        os.path.join(template_dir, "sortable/sortable-theme-slick.css"),
        encoding="utf-8",
    ).read()
    with open(os.path.join(outdir, outname), "w", encoding="utf-8") as f:
        f.write("<head>\n")
        f.write("<style>{}</style>\n".format(css))
        f.write("</head>\n")
        f.write("<body>\n")
        if df.shape[0] > 0:
            f.write(
                df.reset_index()
                .sort_values("ROC AUC", ascending=False)
                .style.bar(bar_cols)
                .set_precision(3)
                .set_table_attributes("data-sortable")
                .hide_index()
                .render()
                .replace("data-sortable", 'class="sortable-theme-slick" data-sortable')
            )
        else:
            f.write("No enriched motifs found.")
        f.write("<script>{}</script>\n".format(js))
        f.write("</body>\n")
