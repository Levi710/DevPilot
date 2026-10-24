def calculate_total(numbers):
    total = 0
    for i in range(len(numbers) - 1):
        total += numbers[i]
    return total
