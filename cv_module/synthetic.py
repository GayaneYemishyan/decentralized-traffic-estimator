
import random


def generate_synthetic_metrics(scenario="free", interval=1.0, step=0):
    if interval <= 0:
        raise ValueError("Interval must be greater than 0")

    if scenario == "free":
        flow_rate = random.randint(200, 600)
        occupancy = random.uniform(0.05, 0.20)

    elif scenario == "rising":
        progress = min(step / 10, 1.0)
        flow_rate = 600 + 600 * progress
        occupancy = 0.25 + 0.55 * progress

    elif scenario == "congested":
        flow_rate = random.randint(400, 900)
        occupancy = random.uniform(0.60, 0.85)

    elif scenario == "jam":
        if step < 5:
            flow_rate = random.randint(200, 600)
            occupancy = random.uniform(0.05, 0.20)
        else:
            flow_rate = random.randint(0, 100)
            occupancy = random.uniform(0.85, 0.98)

    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    # Convert hourly flow rate into vehicles per interval
    expected_count = flow_rate * interval / 3600

    # Keep fractional vehicles probabilistically
    vehicle_count = int(expected_count)
    if random.random() < expected_count - vehicle_count:
        vehicle_count += 1

    # Count vehicles by YOLO class
    counts_by_class = {
        "car": 0,
        "motorcycle": 0,
        "bus": 0,
        "truck": 0
    }

    for _ in range(vehicle_count):
        vehicle_type = random.choices(
            ["car", "motorcycle", "bus", "truck"],
            weights=[70, 10, 10, 10],
            k=1
        )[0]

        counts_by_class[vehicle_type] += 1

    # Calculate actual observed hourly flow
    flow_rate_vph = vehicle_count * 3600 / interval

    return {
        "vehicle_count": vehicle_count,
        "flow_rate_vph": round(flow_rate_vph, 2),
        "avg_occupancy": round(occupancy, 2),
        "counts_by_class": counts_by_class
    }
