package com.example;

/**
 * A shape that is either a circle or a square — nothing else.
 */
public sealed interface Shape permits Circle, Square {
    double area();
}

final class Circle implements Shape {
    double radius;

    Circle(double radius) {
        this.radius = radius;
    }

    /** Pi times radius squared. */
    public double area() {
        return Math.PI * radius * radius;
    }
}

final class Square implements Shape {
    double side;

    Square(double side) {
        this.side = side;
    }

    public double area() {
        return side * side;
    }

    private boolean isUnitSquare() {
        return side == 1.0;
    }
}
