"""Model 3 - direct damage + disruption. Usage: see README.md."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dynamic_flood_costs import (cli, inputs, model1_peak_envelope, model2_duration,  # noqa: E402
                                 model3_disruption, outputs)


def main():
    p = cli.parser(__doc__)
    p.add_argument("--attributes", type=Path, required=True,
                   help="PLACEHOLDER input: CSV buildingid, residents, floor_area_m2")
    p.add_argument("--direct-from", choices=["model1", "model2"], default="model1")
    args = p.parse_args()
    print("Model 3 - direct damage + disruption (unit costs are placeholders)")
    samples, depth, jrc = cli.load(args)
    attributes = inputs.load_attributes(args.attributes, samples)

    model = model1_peak_envelope if args.direct_from == "model1" else model2_duration
    direct = model.compute(depth, samples, jrc)
    result = model3_disruption.compute(direct, depth, samples, attributes)

    series = {"total_cost_eur2010": result["total"], "direct_damage_eur2010": result["direct"],
              "disruption_cost_eur2010": result["disruption"]}
    out = outputs.write(args.out, series, final_extra=attributes[["residents", "floor_area_m2"]])
    outputs.plot(out, {"total_cost_eur2010": result["total"], "direct_damage_eur2010": result["direct"]},
                 "Model 3: disruption keeps adding cost while buildings are wet")
    print(f"  outputs written to {out}")


if __name__ == "__main__":
    main()
