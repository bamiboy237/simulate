---
name: frontend-quality
description: Practical guidance for building or refining user interfaces that are clear, responsive, accessible, and visually coherent. Use for frontend implementation, UI polish, interaction states, accessibility checks, or focused design improvements.
---

# Frontend Quality

Start from the product, audience, and task the interface must support. Inspect the existing design system, components, tokens, content style, and responsive patterns before introducing new ones. Reuse them when they fit. In an established product, improve the requested area without redesigning unrelated screens or replacing a coherent visual language with personal taste.

Give the interface a clear hierarchy. Use spacing, typography, color, alignment, and layout to show what is primary, related, or secondary. Prefer a small set of deliberate choices over many disconnected effects. Decoration should support the content or product character, not compete with it. Avoid generic visual excess such as arbitrary gradients, excessive cards, gratuitous animation, or ornamental controls with no useful meaning.

Use words as interface design. Choose familiar, specific labels based on what the user is trying to do, keep action names consistent through a flow, and make errors and empty states explain the next useful step. Realistic content is better than filler because it reveals wrapping, density, hierarchy, and edge cases.

Design the full interaction, not only the ideal screenshot. Where relevant, handle:

- loading, empty, error, success, and partial-data states;
- disabled, hover, active, selected, and visible focus states;
- long text, validation messages, slow actions, and repeated submission;
- narrow screens, touch input, zoom, and content growth.

Build accessibility into the implementation. Use semantic HTML and native controls where possible. Ensure controls have accessible names, labels are associated with inputs, and keyboard users can reach and operate interactive elements in a sensible order. Maintain readable contrast and visible focus indicators. Do not rely on color alone to communicate meaning. Provide useful alternative text for meaningful images, hide purely decorative images from assistive technology, and respect reduced-motion preferences when motion is present.

Responsive behavior should preserve priority rather than merely shrink the desktop layout. Let content reflow, keep tap targets usable, avoid accidental horizontal scrolling, and test important widths instead of targeting one device. Motion and transitions should clarify change or feedback; keep them restrained and avoid blocking interaction.

Verify visually when the environment allows it. Inspect the changed interface at representative desktop and mobile sizes and exercise keyboard interaction and important states. Use automated accessibility or UI checks when already available, but do not treat them as a substitute for looking at the result and trying the flow. If visual verification is unavailable, say so and identify the states or viewport behavior that still need checking.